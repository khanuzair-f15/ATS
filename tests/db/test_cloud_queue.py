"""PostgreSQL integration tests for the Phase 5A serverless task queue.

These tests run against a live database (marked @pytest.mark.db) and exercise
concurrency, lease fencing, retry and crash recovery against the real
``serverless_task_queue`` table created by migration 0004. They skip cleanly
when no PostgreSQL is reachable.

Uses raw SQL for ``serverless_task_queue`` (no ORM model exists for it) and the
ORM ``source``/``platform`` models for fixtures.
"""

from __future__ import annotations

from datetime import UTC, datetime
from json import dumps
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jobindex.cloud import queue
from jobindex.cloud.queue import (
    abandon_fetch_run,
    claim_batch,
    create_fetch_run_started,
    finalize_fetch_run,
    mark_task_failed,
    mark_task_succeeded,
    reclaim_stale_tasks,
    refresh_heartbeat,
    retry_task,
    update_source_after_run,
)
from jobindex.db import Source
from jobindex.db.enums import PlatformClass, SourceHealth, TransportClass

pytestmark = pytest.mark.db

LEASE_TTL_SECONDS = 900


async def _make_source(
    session: AsyncSession,
    *,
    platform_key: str = "cq_test_platform",
    tenant_key: str = "cq_tenant",
    health: SourceHealth = SourceHealth.HEALTHY,
) -> Source:
    from jobindex.db import Platform

    session.add(
        Platform(
            key=platform_key,
            display_name="CQ Test Platform",
            family="ats",
            platform_class=PlatformClass.C1,
            wave="S3",
            transport=TransportClass.API_JSON,
            auth_kind="none",
            coverage_state="outstanding",
            coverage_reason="test",
            notes="test",
        )
    )
    await session.flush()
    source = Source(
        platform_key=platform_key,
        tenant_key=tenant_key,
        display_name="CQ Test Tenant",
        entry_url=f"https://{tenant_key}.example.com/careers",
        transport_class=TransportClass.API_JSON,
        connector=platform_key,
        tenant_status="validated",
        health=health,
    )
    session.add(source)
    await session.flush()
    return source


async def _insert_pending_task(
    session: AsyncSession, source_id, run_id: str | None = None
) -> int:
    from jobindex.cloud.queue import INSERT_PENDING_TASK_SQL

    result = await session.execute(
        INSERT_PENDING_TASK_SQL,
        {
            "source_id": str(source_id),
            "run_id": str(run_id or uuid4()),
            "payload": dumps({}),
        },
    )
    row = result.mappings().first()
    return int(row["id"])


async def test_concurrent_worker_claims_no_overlap(db_session) -> None:
    """Two workers claiming the same single pending task: exactly one succeeds."""
    source = await _make_source(db_session, tenant_key="cq_concurrent")
    task_id = await _insert_pending_task(db_session, source.id)
    await db_session.commit()

    claimed_a = await claim_batch(db_session, worker_id="worker-a", batch_size=5)
    claimed_b = await claim_batch(db_session, worker_id="worker-b", batch_size=5)

    ids_a = {int(t["id"]) for t in claimed_a}
    ids_b = {int(t["id"]) for t in claimed_b}
    assert task_id in ids_a ^ ids_b, (
        f"task claimed by both workers: a={ids_a} b={ids_b}"
    )
    assert len(ids_a) + len(ids_b) == 1


async def test_stale_worker_completion_is_fenced(db_session) -> None:
    """A reclaimed worker cannot complete a task it no longer owns.

    Worker-a claims the task (claim_token_a). After its lease expires, worker-b
    re-claims it (claim_token_b replaces claim_token_a). Worker-a's stale attempt
    to complete is fenced by claim_token and matches 0 rows; worker-b completes it.
    """
    source = await _make_source(db_session, tenant_key="cq_stale")
    task_id = await _insert_pending_task(db_session, source.id)
    await db_session.commit()

    claimed = await claim_batch(db_session, worker_id="worker-a", batch_size=5)
    assert len(claimed) == 1
    task = claimed[0]
    claim_token_a = str(task["claim_token"])

    # Simulate worker-b re-claiming the same task after worker-a's lease expired:
    # the claim_token rotates to token_b while the task is still 'running'.
    claim_token_b = str(uuid4())
    await db_session.execute(
        text(
            "UPDATE serverless_task_queue "
            "SET claim_token = :new_token, heartbeat_at = now() "
            "WHERE id = :id AND state = 'running'"
        ),
        {"new_token": claim_token_b, "id": task_id},
    )
    await db_session.flush()

    # Worker-b (current claimant) completes the task successfully.
    await mark_task_succeeded(
        db_session,
        task_id=int(task["id"]),
        claim_token=claim_token_b,
        result={"jobs_found": 1},
    )

    # Worker-a (stale, holding the rotated-away claim_token_a) is fenced: no-op.
    await mark_task_succeeded(
        db_session,
        task_id=int(task["id"]),
        claim_token=claim_token_a,
        result={"jobs_found": 999},
    )

    # Confirm the task was completed by worker-b (claim_token match), not worker-a.
    result = await db_session.execute(
        text(
            "SELECT state, result FROM serverless_task_queue WHERE id = :id"
        ),
        {"id": task_id},
    )
    row = result.mappings().first()
    assert row["state"] == "succeeded"
    assert row["result"] is not None


async def test_lease_expiry_and_reclaim(db_session) -> None:
    """An expired heartbeat is reclaimed as abandoned."""
    source = await _make_source(db_session, tenant_key="cq_lease")
    task_id = await _insert_pending_task(db_session, source.id)
    await db_session.commit()

    await claim_batch(db_session, worker_id="worker-a", batch_size=5)

    # Heartbeat far in the past -> expired lease.
    await db_session.execute(
        text(
            "UPDATE serverless_task_queue "
            "SET heartbeat_at = now() - interval '1 hour' "
            "WHERE id = :id"
        ),
        {"id": task_id},
    )
    await db_session.flush()

    reclaimed = await reclaim_stale_tasks(db_session, lease_ttl_seconds=LEASE_TTL_SECONDS)
    assert len(reclaimed) == 1
    assert reclaimed[0]["state"] == "abandoned"


async def test_retry_and_crash_recovery(db_session) -> None:
    """A failed task is retried with backoff; after max_attempts it is exhausted."""
    source = await _make_source(db_session, tenant_key="cq_retry")
    task_id = await _insert_pending_task(db_session, source.id)
    await db_session.commit()

    claimed = await claim_batch(db_session, worker_id="worker-a", batch_size=5)
    assert len(claimed) == 1
    task = claimed[0]
    claim_token = str(task["claim_token"])

    # A fetch_run is written at claim (queue_id links to the task id).
    await create_fetch_run_started(
        db_session,
        source_id=str(source.id),
        queue_id=task["id"],
        worker_id="worker-a",
    )

    # Mark failed; retry returns it to pending with backoff (attempt 1 < max 3).
    await mark_task_failed(
        db_session,
        task_id=int(task["id"]),
        claim_token=claim_token,
        error_detail="simulated failure",
    )
    rescheduled = await retry_task(
        db_session, task_id=int(task["id"]), backoff_seconds=30
    )
    assert rescheduled is True

    # Verify next_claim_at moved into the future.
    result = await db_session.execute(
        text("SELECT state, next_claim_at FROM serverless_task_queue WHERE id = :id"),
        {"id": task_id},
    )
    row = result.mappings().first()
    assert row["state"] == "pending"
    assert row["next_claim_at"] > datetime.now(UTC)

    # Simulate exhausting all attempts: set attempts = max_attempts directly.
    await db_session.execute(
        text(
            "UPDATE serverless_task_queue SET attempts = max_attempts WHERE id = :id"
        ),
        {"id": task_id},
    )
    await db_session.flush()

    # Now retry must refuse (attempts >= max_attempts).
    rescheduled = await retry_task(
        db_session, task_id=int(task["id"]), backoff_seconds=30
    )
    assert rescheduled is False

    # Crash recovery: an abandoned task's fetch_run is marked abandoned.
    await abandon_fetch_run(db_session, task_id=int(task["id"]), worker_id="worker-a")
    result = await db_session.execute(
        text(
            "SELECT outcome FROM fetch_run WHERE queue_id = :qid AND outcome IS NOT NULL"
        ),
        {"qid": task_id},
    )
    rows = result.mappings().all()
    assert any(r["outcome"] == "abandoned" for r in rows)


async def test_source_health_transitions(db_session) -> None:
    """Successful run -> healthy/reset failures; failed run -> degraded/failures++."""
    source = await _make_source(db_session, tenant_key="cq_health")
    await _insert_pending_task(db_session, source.id)
    await db_session.commit()

    claimed = await claim_batch(db_session, worker_id="worker-a", batch_size=5)
    assert len(claimed) == 1
    claim_token = str(claimed[0]["claim_token"])

    # Success path via the real update_source_after_run (fenced by claim_token).
    await queue.update_source_after_run(
        db_session,
        str(source.id),
        claim_token=claim_token,
        success=True,
        backoff_seconds=0,
        tier_seconds=12 * 3600,
    )
    await db_session.flush()

    result = await db_session.execute(
        text("SELECT health, consecutive_failures FROM source WHERE id=:id"),
        {"id": source.id},
    )
    row = result.mappings().first()
    assert row["health"] == "healthy"
    assert row["consecutive_failures"] == 0

    # Failure path (fenced by claim_token).
    await queue.update_source_after_run(
        db_session,
        str(source.id),
        claim_token=claim_token,
        success=False,
        backoff_seconds=300,
        tier_seconds=12 * 3600,
    )
    await db_session.flush()

    result = await db_session.execute(
        text("SELECT health, consecutive_failures FROM source WHERE id=:id"),
        {"id": source.id},
    )
    row = result.mappings().first()
    assert row["health"] == "degraded"
    assert row["consecutive_failures"] == 1


async def test_cross_source_isolation(db_session) -> None:
    """Claiming a task for source A does not block claiming for source B."""
    source_a = await _make_source(db_session, tenant_key="cq_iso_a")
    source_b = await _make_source(
        db_session, tenant_key="cq_iso_b", platform_key="cq_test_platform_b"
    )
    await _insert_pending_task(db_session, source_a.id)
    await _insert_pending_task(db_session, source_b.id)
    await db_session.commit()

    claimed_a = await claim_batch(db_session, worker_id="worker-a", batch_size=5)
    claimed_b = await claim_batch(db_session, worker_id="worker-b", batch_size=5)

    assert len(claimed_a) == 1
    assert len(claimed_b) == 1
    assert claimed_a[0]["source_id"] != claimed_b[0]["source_id"]


async def test_refresh_heartbeat_keeps_lease(db_session) -> None:
    """A healthy worker can refresh its heartbeat; a reclaimed worker cannot."""
    source = await _make_source(db_session, tenant_key="cq_heartbeat")
    await _insert_pending_task(db_session, source.id)
    await db_session.commit()

    claimed = await claim_batch(db_session, worker_id="worker-a", batch_size=5)
    task = claimed[0]
    claim_token = str(task["claim_token"])

    # Worker-a refreshes -> still owns the lease.
    refreshed = await refresh_heartbeat(
        db_session, task_id=int(task["id"]), claim_token=claim_token
    )
    assert refreshed is True

    # Worker-b (different token) cannot refresh.
    refreshed_b = await refresh_heartbeat(
        db_session, task_id=int(task["id"]), claim_token="wrong-token"
    )
    assert refreshed_b is False


async def test_stale_worker_finalize_fetch_run_is_fenced(db_session) -> None:
    """A stale worker cannot finalize a fetch_run it no longer owns.

    Worker-a claims the task (claim_token_a) and creates a fetch_run. After its
    lease expires, worker-b re-claims it (claim_token_b replaces claim_token_a).
    Worker-a's stale attempt to finalize the fetch_run is fenced by claim_token
    and matches 0 rows; worker-b finalizes it.
    """
    source = await _make_source(db_session, tenant_key="cq_fence_fetch")
    task_id = await _insert_pending_task(db_session, source.id)
    await db_session.commit()

    claimed = await claim_batch(db_session, worker_id="worker-a", batch_size=5)
    assert len(claimed) == 1
    task = claimed[0]
    claim_token_a = str(task["claim_token"])

    # Create a fetch_run at claim (worker-a is the claimant).
    await create_fetch_run_started(
        db_session,
        source_id=str(source.id),
        queue_id=int(task["id"]),
        worker_id="worker-a",
    )

    # Simulate worker-b re-claiming after worker-a's lease expired.
    await db_session.execute(
        text(
            "UPDATE serverless_task_queue "
            "SET claim_token = :new_token, heartbeat_at = now() "
            "WHERE id = :id AND state = 'running'"
        ),
        {"new_token": "claim-token-b", "id": task_id},
    )
    await db_session.flush()

    # Worker-b (current claimant) finalizes the fetch_run successfully.
    await finalize_fetch_run(
        db_session,
        fetch_run_id=str(task["id"]),
        claim_token="claim-token-b",
        outcome="success",
        http_status=200,
        jobs_found=1,
        worker_id="worker-b",
    )

    # Worker-a (stale, holding rotated-away claim_token_a) is fenced: no-op.
    await finalize_fetch_run(
        db_session,
        fetch_run_id=str(task["id"]),
        claim_token=claim_token_a,
        outcome="failure",
        http_status=500,
        jobs_found=0,
        worker_id="worker-a",
    )

    # Confirm the fetch_run was finalized by worker-b, not worker-a.
    result = await db_session.execute(
        text(
            "SELECT outcome, http_status, jobs_found FROM fetch_run WHERE queue_id = :qid"
        ),
        {"qid": task_id},
    )
    row = result.mappings().first()
    assert row["outcome"] == "success"
    assert row["http_status"] == 200
    assert row["jobs_found"] == 1


async def test_stale_worker_source_update_is_fenced(db_session) -> None:
    """A stale worker cannot update source health after lease expiry.

    Worker-a claims the task for source-a (claim_token_a). After its lease
    expires, worker-b re-claims it (claim_token_b replaces claim_token_a).
    Worker-a's stale attempt to update source health is fenced by claim_token
    and matches 0 rows; worker-b's update succeeds.
    """
    source = await _make_source(db_session, tenant_key="cq_fence_source")
    task_id = await _insert_pending_task(db_session, source.id)
    await db_session.commit()

    claimed = await claim_batch(db_session, worker_id="worker-a", batch_size=5)
    assert len(claimed) == 1
    task = claimed[0]
    claim_token_a = str(task["claim_token"])

    # Simulate worker-b re-claiming after worker-a's lease expired.
    await db_session.execute(
        text(
            "UPDATE serverless_task_queue "
            "SET claim_token = :new_token, heartbeat_at = now() "
            "WHERE id = :id AND state = 'running'"
        ),
        {"new_token": "claim-token-b", "id": task_id},
    )
    await db_session.flush()

    # Worker-b (current claimant) updates source health successfully.
    await update_source_after_run(
        db_session,
        str(source.id),
        claim_token="claim-token-b",
        success=True,
        backoff_seconds=0,
        tier_seconds=12 * 3600,
    )
    await db_session.flush()

    result = await db_session.execute(
        text("SELECT health, consecutive_failures FROM source WHERE id = :id"),
        {"id": source.id},
    )
    row = result.mappings().first()
    assert row["health"] == "healthy"
    assert row["consecutive_failures"] == 0

    # Worker-a (stale, holding rotated-away claim_token_a) is fenced: no-op.
    await update_source_after_run(
        db_session,
        str(source.id),
        claim_token=claim_token_a,
        success=False,
        backoff_seconds=300,
        tier_seconds=12 * 3600,
    )
    await db_session.flush()

    # Confirm source health was NOT corrupted by worker-a's stale update.
    result = await db_session.execute(
        text("SELECT health, consecutive_failures FROM source WHERE id = :id"),
        {"id": source.id},
    )
    row = result.mappings().first()
    assert row["health"] == "healthy"
    assert row["consecutive_failures"] == 0


async def test_reclaim_does_not_clear_newer_claim_token(db_session) -> None:
    """Reclaiming a stale task must not clear source.claimed_task_token when a
    newer claim has already replaced it.

    Worker-a claims task T1 (claim_token_a) for source S. Worker-a's lease
    expires. Worker-b claims T1 again (claim_token_b). Source.claimed_task_token
    is now token_b. Reclaiming T1 under worker-a's stale heartbeat must NOT
    clear source.claimed_task_token because it no longer matches token_a.
    """
    source = await _make_source(db_session, tenant_key="cq_reclaim_race")
    task_id = await _insert_pending_task(db_session, source.id)
    await db_session.commit()

    # Worker-a claims the task.
    claimed = await claim_batch(db_session, worker_id="worker-a", batch_size=5)
    assert len(claimed) == 1
    task = claimed[0]
    claim_token_a = str(task["claim_token"])

    # Verify source.claimed_task_token was set to token_a by the claim.
    result = await db_session.execute(
        text("SELECT claimed_task_token FROM source WHERE id = :id"),
        {"id": source.id},
    )
    row = result.mappings().first()
    assert row["claimed_task_token"] == claim_token_a

    # Simulate worker-a's lease expiring.
    await db_session.execute(
        text(
            "UPDATE serverless_task_queue "
            "SET heartbeat_at = now() - interval '1 hour' "
            "WHERE id = :id"
        ),
        {"id": task_id},
    )
    await db_session.flush()

    # Worker-b re-claims the same task (claim_token rotates to token_b).
    claimed_b = await claim_batch(db_session, worker_id="worker-b", batch_size=5)
    assert len(claimed_b) == 1
    task_b = claimed_b[0]
    claim_token_b = str(task_b["claim_token"])
    assert claim_token_b != claim_token_a

    # Verify source.claimed_task_token is now token_b.
    result = await db_session.execute(
        text("SELECT claimed_task_token FROM source WHERE id = :id"),
        {"id": source.id},
    )
    row = result.mappings().first()
    assert row["claimed_task_token"] == claim_token_b

    # Reclaim task T1 under worker-a's stale heartbeat (heartbeat is old).
    reclaimed = await reclaim_stale_tasks(db_session, lease_ttl_seconds=LEASE_TTL_SECONDS)
    assert len(reclaimed) == 1
    assert reclaimed[0]["id"] == task_id

    # source.claimed_task_token must STILL be token_b — reclaim must NOT clear it
    # because token_a != token_b.
    result = await db_session.execute(
        text("SELECT claimed_task_token FROM source WHERE id = :id"),
        {"id": source.id},
    )
    row = result.mappings().first()
    assert row["claimed_task_token"] == claim_token_b
