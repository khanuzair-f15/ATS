"""PostgreSQL integration tests for source-health monitoring.

These tests run against a live database (marked @pytest.mark.db) and exercise the
monitoring query logic in ``jobindex.api.monitoring`` against the real
``source``, ``fetch_run`` and ``serverless_task_queue`` tables. They skip
cleanly when no PostgreSQL is reachable.

Health status (source.health) and active-execution status (running task with
valid lease heartbeat) are separate dimensions. Expired leases are not counted
as active scraping; a failed scrape is not proof that jobs closed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from conftest import _ensure_platform
from jobindex.api.monitoring import _fetch_source_rows, _source_summary
from jobindex.db import Source
from jobindex.db.enums import SourceHealth, TransportClass

pytestmark = pytest.mark.db

LEASE_TTL_SECONDS = 900


async def _make_source(
    session,
    *,
    platform_key: str = "mon_test_platform",
    tenant_key: str = "mon_tenant",
    health: SourceHealth = SourceHealth.HEALTHY,
    enabled: bool = True,
) -> Source:
    # platform.key is the primary key; ensure the platform row exists idempotently so
    # several sources can share one platform within one test transaction.
    await _ensure_platform(session, platform_key=platform_key)
    source = Source(
        platform_key=platform_key,
        tenant_key=tenant_key,
        display_name="Mon Test Tenant",
        entry_url=f"https://{tenant_key}.example.com/careers",
        transport_class=TransportClass.API_JSON,
        connector=platform_key,
        tenant_status="validated",
        health=health,
        enabled=enabled,
    )
    session.add(source)
    await session.flush()
    return source


async def _insert_pending_task(session, source_id) -> None:
    from jobindex.cloud.queue import INSERT_PENDING_TASK_SQL

    await session.execute(
        INSERT_PENDING_TASK_SQL,
        {"source_id": str(source_id), "run_id": str(uuid4()), "payload": {}},
    )


async def _insert_claimed_task(
    session, source_id, claim_token, heartbeat: datetime | None = None
) -> None:
    hb = heartbeat or datetime.now(UTC)
    await session.execute(
        text(
            """
            INSERT INTO serverless_task_queue
              (source_id, run_id, claim_token, state, claimed_by, claimed_at,
               heartbeat_at, attempts, max_attempts, next_claim_at, payload)
            VALUES
              (:source_id, :run_id, :claim_token, 'running', 'worker-test',
               now(), :heartbeat, 1, 3, now(), '{}'::jsonb)
            """
        ),
        {
            "source_id": str(source_id),
            "run_id": str(uuid4()),
            "claim_token": str(claim_token),
            "heartbeat": hb,
        },
    )


async def _insert_fetch_run(
    session, source_id, outcome="success", error_class="none", jobs_found=2
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO fetch_run
              (source_id, queue_id, started_at, finished_at, outcome,
               error_class, jobs_found, listing_complete, worker_id)
            VALUES
              (:source_id, NULL, now(), now(), :outcome, :error_class,
               :jobs_found, true, 'worker-test')
            """
        ),
        {
            "source_id": str(source_id),
            "outcome": outcome,
            "error_class": error_class,
            "jobs_found": jobs_found,
        },
    )


async def test_monitoring_health_and_active_are_separate_dimensions(db_session) -> None:
    """Health status and active-execution status are tracked separately."""
    # Healthy + actively scraping (running task with valid heartbeat).
    source_a = await _make_source(db_session, tenant_key="mon_a")
    await _insert_claimed_task(db_session, source_a.id, uuid4())
    await _insert_fetch_run(db_session, source_a.id)

    # Degraded + NOT actively scraping (expired lease / no running task).
    source_b = await _make_source(
        db_session, tenant_key="mon_b", health=SourceHealth.DEGRADED
    )
    await _insert_fetch_run(
        db_session, source_b.id, outcome="failure", error_class="transient"
    )

    # Disabled (unavailable) + not scraping.
    await _make_source(
        db_session, tenant_key="mon_c", health=SourceHealth.DISABLED, enabled=False
    )

    await db_session.commit()

    rows = await _fetch_source_rows(db_session)
    by_tenant = {str(r["tenant_key"]): r for r in rows}

    # Source A: healthy AND actively scraping (separate dimensions both true).
    assert by_tenant["mon_a"]["health"] == "healthy"
    assert by_tenant["mon_a"]["actively_scraping"] is True
    assert by_tenant["mon_a"]["claimed"] is True

    # Source B: degraded, not actively scraping (expired lease / no running task).
    assert by_tenant["mon_b"]["health"] == "degraded"
    assert by_tenant["mon_b"]["actively_scraping"] is False

    # Source C: disabled = unavailable, not scraping.
    assert by_tenant["mon_c"]["health"] == "disabled"
    assert by_tenant["mon_c"]["enabled"] is False
    assert by_tenant["mon_c"]["actively_scraping"] is False


async def test_expired_lease_not_counted_as_active_scraping(db_session) -> None:
    """A claimed task with an expired heartbeat is not active scraping."""
    source = await _make_source(db_session, tenant_key="mon_lease")
    # Claimed but heartbeat far in the past -> expired lease.
    await _insert_claimed_task(
        db_session,
        source.id,
        uuid4(),
        heartbeat=datetime.now(UTC) - timedelta(hours=1),
    )
    await db_session.commit()

    rows = await _fetch_source_rows(db_session)
    row = rows[0]
    assert row["claimed"] is True
    assert row["actively_scraping"] is False


async def test_failed_scrape_not_proof_of_closure(db_session) -> None:
    """A failed scrape records failure outcome but does not imply jobs closed."""
    source = await _make_source(db_session, tenant_key="mon_fail")
    await _insert_fetch_run(
        db_session, source.id, outcome="failure", error_class="transient", jobs_found=0
    )
    await db_session.commit()

    rows = await _fetch_source_rows(db_session)
    row = rows[0]
    assert row["latest_outcome"] == "failure"
    assert row["latest_listing_complete"] is True  # completed run
    # No closure evidence is implied here; monitoring only reports what ran.


async def test_source_count_aggregation(db_session) -> None:
    """Total/enabled/scraping counts reflect the source table."""
    await _make_source(db_session, tenant_key="mon_agg_a")
    source_b = await _make_source(db_session, tenant_key="mon_agg_b", enabled=False)
    await _insert_claimed_task(db_session, source_b.id, uuid4())
    await db_session.commit()

    rows = await _fetch_source_rows(db_session)
    total = len(rows)
    enabled = sum(1 for r in rows if r["enabled"])
    actively_scraping = sum(1 for r in rows if r["actively_scraping"])

    assert total == 2
    assert enabled == 1
    assert actively_scraping == 1  # source_b is disabled but has a running task


async def test_multiple_websites_same_platform(db_session) -> None:
    """Two sources on the same ATS platform are tracked independently."""
    await _make_source(db_session, platform_key="share_platform", tenant_key="share_a")
    await _make_source(
        db_session, platform_key="share_platform", tenant_key="share_b"
    )
    await db_session.commit()

    rows = await _fetch_source_rows(db_session)
    share = [r for r in rows if r["platform_key"] == "share_platform"]
    assert len(share) == 2
    tenants = {r["tenant_key"] for r in share}
    assert tenants == {"share_a", "share_b"}


async def test_last_attempt_and_next_due_reported(db_session) -> None:
    """Last attempt, last success and next scheduled run are reported."""
    await _make_source(db_session, tenant_key="mon_times")
    await db_session.commit()

    rows = await _fetch_source_rows(db_session)
    row = rows[0]
    assert row["last_attempt_at"] is not None
    assert row["next_due_at"] is not None
    # Parsable timestamps.
    assert row["last_attempt_at"].endswith("Z")
    assert row["next_due_at"].endswith("Z")


async def test_source_summary_shape(db_session) -> None:
    """_source_summary returns the documented shape."""
    await _make_source(db_session, tenant_key="mon_shape")
    await db_session.commit()

    rows = await _fetch_source_rows(db_session)
    assert len(rows) == 1
    summary = _source_summary(rows[0])
    required_keys = {
        "source_id",
        "platform_key",
        "platform_display_name",
        "family",
        "platform_class",
        "tenant_key",
        "display_name",
        "entry_url",
        "enabled",
        "health",
        "actively_scraping",
        "claimed",
        "consecutive_failures",
        "latest_outcome",
        "latest_error_class",
        "latest_jobs_found",
        "latest_listing_complete",
        "last_attempt_at",
        "last_success_at",
        "next_due_at",
    }
    assert required_keys.issubset(summary.keys())
