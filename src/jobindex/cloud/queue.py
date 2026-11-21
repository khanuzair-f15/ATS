"""Serverless task queue operations.

All SQL is Postgres-native and mirrors the approved source_run_queue / fetch_run
pattern (ADR 0003, docs/schema.md §7, §10). Statements are short, transactional,
and safe to run inside short-lived Vercel serverless invocations.

These functions are deliberately free of business logic: they execute SQL and
return rows. The orchestrator in `worker.py` decides retries, backoff and state
transitions, which keeps this module easy to test with a mocked session.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "abandon_fetch_run",
    "claim_batch",
    "create_fetch_run_started",
    "finalize_fetch_run",
    "insert_pending_task",
    "log_event",
    "mark_task_failed",
    "mark_task_succeeded",
    "reclaim_stale_tasks",
    "refresh_heartbeat",
    "retry_task",
    "update_source_after_run",
]

# ---------------------------------------------------------------------------
# SQL statements
# ---------------------------------------------------------------------------

CLAIM_BATCH_SQL = text(
    """
    WITH claimed AS (
        UPDATE serverless_task_queue q
        SET state = 'running',
            claim_token = gen_random_uuid(),
            claimed_by = :worker_id,
            claimed_at = now(),
            heartbeat_at = now(),
            attempts = q.attempts + 1
        WHERE q.id IN (
            SELECT q2.id
            FROM serverless_task_queue q2
            WHERE q2.state = 'pending'
              AND q2.next_claim_at <= now()
            ORDER BY q2.next_claim_at ASC
            FOR UPDATE OF q2 SKIP LOCKED
            LIMIT :batch_size
        )
        RETURNING q.id, q.source_id, q.run_id, q.claim_token, q.task_kind, q.payload,
                  q.attempts, q.max_attempts
    )
    UPDATE source s
    SET claimed_task_token = c.claim_token
    FROM claimed c
    WHERE s.id = c.source_id
    RETURNING c.id, c.source_id, c.run_id, c.claim_token, c.task_kind, c.payload,
              c.attempts, c.max_attempts
    """
)

INSERT_FETCH_RUN_STARTED_SQL = text(
    """
    INSERT INTO fetch_run (
        source_id, queue_id, started_at, outcome, error_class, worker_id
    ) VALUES (
        :source_id, :queue_id, now(), NULL, 'none', :worker_id
    )
    RETURNING id, started_at
    """
)

FINALIZE_FETCH_RUN_SQL = text(
    """
    UPDATE fetch_run f
    SET finished_at = now(),
        outcome = :outcome,
        http_status = :http_status,
        error_class = :error_class,
        error_detail = :error_detail,
        jobs_found = :jobs_found,
        jobs_new = :jobs_new,
        jobs_updated = :jobs_updated,
        jobs_stale = :jobs_stale,
        jobs_closed = :jobs_closed,
        pages_fetched = :pages_fetched,
        pages_expected = :pages_expected,
        listing_complete = :listing_complete,
        duration_ms = :duration_ms,
        worker_id = :worker_id
    FROM serverless_task_queue q
    WHERE f.queue_id = q.id
      AND q.claim_token = :claim_token
      AND f.outcome IS NULL
    RETURNING f.id
    """
)

# Fence source health updates on the current claim_token so a stale/reclaimed
# worker cannot corrupt source state after its lease expires.
UPDATE_SOURCE_AFTER_RUN_SQL = text(
    """
    UPDATE source
    SET last_attempt_at = now(),
        last_success_at = CASE WHEN :success THEN now() ELSE last_success_at END,
        consecutive_failures = CASE
            WHEN :success THEN 0
            ELSE consecutive_failures + 1
        END,
        health = CASE WHEN :success THEN 'healthy' ELSE 'degraded' END,
        backoff_until = CASE
            WHEN :success THEN NULL
            ELSE now() + (:backoff_seconds * interval '1 second')
        END,
        next_due_at = CASE
            WHEN :success THEN now() + (:tier_seconds * interval '1 second')
            ELSE LEAST(next_due_at, now() + (:backoff_seconds * interval '1 second'))
        END,
        claimed_task_token = NULL
    WHERE id = :source_id
      AND claimed_task_token = :claim_token
    """
)

MARK_TASK_SUCCEEDED_SQL = text(
    """
    UPDATE serverless_task_queue
    SET state = 'succeeded',
        completed_at = now(),
        result = :result,
        error_detail = NULL
    WHERE id = :task_id
      AND state = 'running'
      AND claim_token = :claim_token
    """
)

MARK_TASK_FAILED_SQL = text(
    """
    UPDATE serverless_task_queue
    SET state = 'failed',
        completed_at = now(),
        result = NULL,
        error_detail = :error_detail
    WHERE id = :task_id
      AND state = 'running'
      AND claim_token = :claim_token
    """
)

RETRY_TASK_SQL = text(
    """
    UPDATE serverless_task_queue
    SET state = 'pending',
        claimed_by = NULL,
        claimed_at = NULL,
        heartbeat_at = NULL,
        next_claim_at = now() + (:backoff_seconds * interval '1 second')
    WHERE id = :task_id
      AND state = 'failed'
      AND attempts < max_attempts
    RETURNING id
    """
)

REFRESH_HEARTBEAT_SQL = text(
    """
    UPDATE serverless_task_queue
    SET heartbeat_at = now()
    WHERE id = :task_id
      AND state = 'running'
      AND claim_token = :claim_token
    RETURNING id
    """
)

RECLAIM_STALE_TASKS_SQL = text(
    """
    WITH reclaimed AS (
        UPDATE serverless_task_queue
        SET state = 'abandoned',
            claimed_by = NULL,
            claimed_at = NULL,
            heartbeat_at = NULL
        WHERE state = 'running'
          AND heartbeat_at < now() - (:lease_ttl_seconds * interval '1 second')
        RETURNING id, source_id, run_id, claim_token
    )
    UPDATE source s
    SET claimed_task_token = NULL
    FROM reclaimed r
    WHERE s.id = r.source_id
      AND s.claimed_task_token = r.claim_token
    RETURNING r.id, r.source_id, r.run_id
    """
)

ABANDON_FETCH_RUN_SQL = text(
    """
    UPDATE fetch_run f
    SET outcome = 'abandoned',
        finished_at = now(),
        error_class = 'transient',
        error_detail = 'worker heartbeat expired',
        worker_id = :worker_id
    WHERE f.queue_id = :task_id
      AND f.outcome IS NULL
    RETURNING f.id
    """
)

LOG_EVENT_SQL = text(
    """
    INSERT INTO serverless_execution_log (task_id, run_id, worker_id, event, detail)
    VALUES (:task_id, :run_id, :worker_id, :event, :detail)
    """
)

INSERT_PENDING_TASK_SQL = text(
    """
    INSERT INTO serverless_task_queue (source_id, run_id, task_kind, payload, next_claim_at)
    VALUES (:source_id, :run_id, 'refresh', :payload, now())
    RETURNING id
    """
)


# ---------------------------------------------------------------------------
# Thin async wrappers
# ---------------------------------------------------------------------------


async def claim_batch(
    session: AsyncSession,
    worker_id: str,
    batch_size: int,
) -> list[dict[str, Any]]:
    """Claim up to ``batch_size`` pending tasks atomically (SKIP LOCKED).

    Each claimed task is marked ``running`` with its attempt counter
    incremented, so a crashed worker never loses the attempt record and no two
    workers process the same source concurrently.
    """
    result = await session.execute(
        CLAIM_BATCH_SQL, {"worker_id": worker_id, "batch_size": batch_size}
    )
    return [dict(row) for row in result.mappings()]


async def create_fetch_run_started(
    session: AsyncSession,
    source_id: str,
    queue_id: int,
    worker_id: str,
) -> str:
    """Write a fetch_run at claim/start (outcome NULL). Returns the run id."""
    result = await session.execute(
        INSERT_FETCH_RUN_STARTED_SQL,
        {"source_id": source_id, "queue_id": queue_id, "worker_id": worker_id},
    )
    row = result.mappings().first()
    if row is None:  # pragma: no cover - INSERT ... RETURNING always yields a row
        raise RuntimeError("fetch_run INSERT RETURNING produced no row")
    return str(row["id"])


async def finalize_fetch_run(
    session: AsyncSession,
    fetch_run_id: str,
    claim_token: str,
    *,
    outcome: str,
    http_status: int | None = None,
    error_class: str = "none",
    error_detail: str | None = None,
    jobs_found: int = 0,
    jobs_new: int = 0,
    jobs_updated: int = 0,
    jobs_stale: int = 0,
    jobs_closed: int = 0,
    pages_fetched: int | None = None,
    pages_expected: int | None = None,
    listing_complete: bool = False,
    duration_ms: int | None = None,
    worker_id: str,
) -> None:
    """Finalize the fetch_run row created at claim/start.

    Fenced by ``claim_token`` so a stale/reclaimed worker cannot overwrite
    another worker's completed run.
    """
    await session.execute(
        FINALIZE_FETCH_RUN_SQL,
        {
            "fetch_run_id": fetch_run_id,
            "outcome": outcome,
            "http_status": http_status,
            "error_class": error_class,
            "error_detail": error_detail,
            "jobs_found": jobs_found,
            "jobs_new": jobs_new,
            "jobs_updated": jobs_updated,
            "jobs_stale": jobs_stale,
            "jobs_closed": jobs_closed,
            "pages_fetched": pages_fetched,
            "pages_expected": pages_expected,
            "listing_complete": listing_complete,
            "duration_ms": duration_ms,
            "worker_id": worker_id,
        },
    )


async def update_source_after_run(
    session: AsyncSession,
    source_id: str,
    claim_token: str,
    *,
    success: bool,
    backoff_seconds: int,
    tier_seconds: int,
) -> None:
    """Update source health, backoff, and next-due scheduling after a run.

    Fenced by ``claim_token`` so a stale/reclaimed worker cannot corrupt
    source state after its lease expires.
    """
    await session.execute(
        UPDATE_SOURCE_AFTER_RUN_SQL,
        {
            "source_id": source_id,
            "claim_token": claim_token,
            "success": success,
            "backoff_seconds": backoff_seconds,
            "tier_seconds": tier_seconds,
        },
    )


async def mark_task_succeeded(
    session: AsyncSession,
    task_id: int,
    claim_token: str,
    result: dict[str, Any] | None = None,
) -> None:
    """Mark a claimed task as succeeded, persisting its result.

    Fenced by ``claim_token`` so a stale/reclaimed worker cannot overwrite.
    """
    await session.execute(
        MARK_TASK_SUCCEEDED_SQL,
        {"task_id": task_id, "claim_token": claim_token, "result": result},
    )


async def mark_task_failed(
    session: AsyncSession,
    task_id: int,
    claim_token: str,
    error_detail: str | None = None,
) -> None:
    """Mark a claimed task as failed, persisting the error detail.

    Fenced by ``claim_token`` so a stale/reclaimed worker cannot overwrite.
    """
    await session.execute(
        MARK_TASK_FAILED_SQL,
        {"task_id": task_id, "claim_token": claim_token, "error_detail": error_detail},
    )


async def retry_task(
    session: AsyncSession, task_id: int, backoff_seconds: int
) -> bool:
    """Return a failed task to pending with backoff, if attempts remain.

    Returns True when the task was re-scheduled for retry.
    """
    result = await session.execute(
        RETRY_TASK_SQL,
        {"task_id": task_id, "backoff_seconds": backoff_seconds},
    )
    return result.mappings().first() is not None


async def refresh_heartbeat(
    session: AsyncSession, task_id: int, claim_token: str
) -> bool:
    """Refresh the lease heartbeat for a task the worker still owns.

    Returns True when the heartbeat was updated (worker still owns the running
    task), False when the task was already reclaimed by another worker.
    """
    result = await session.execute(
        REFRESH_HEARTBEAT_SQL,
        {"task_id": task_id, "claim_token": claim_token},
    )
    return result.mappings().first() is not None


async def reclaim_stale_tasks(
    session: AsyncSession, lease_ttl_seconds: int
) -> list[dict[str, Any]]:
    """Mark running tasks whose lease expired as abandoned (worker crash recovery)."""
    result = await session.execute(
        RECLAIM_STALE_TASKS_SQL, {"lease_ttl_seconds": lease_ttl_seconds}
    )
    return [dict(row) for row in result.mappings()]


async def abandon_fetch_run(
    session: AsyncSession, task_id: int, worker_id: str
) -> None:
    """Finalize an in-progress fetch_run as abandoned (worker crash recovery)."""
    await session.execute(
        ABANDON_FETCH_RUN_SQL, {"task_id": task_id, "worker_id": worker_id}
    )


async def log_event(
    session: AsyncSession,
    task_id: int,
    run_id: str,
    worker_id: str,
    event: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append one structured execution-log row."""
    await session.execute(
        LOG_EVENT_SQL,
        {
            "task_id": task_id,
            "run_id": run_id,
            "worker_id": worker_id,
            "event": event,
            "detail": detail or {},
        },
    )


async def insert_pending_task(
    session: AsyncSession,
    source_id: str,
    run_id: str,
    payload: dict[str, Any],
) -> int:
    """Insert one pending task (used by the dispatcher / tests)."""
    result = await session.execute(
        INSERT_PENDING_TASK_SQL,
        {"source_id": source_id, "run_id": run_id, "payload": payload},
    )
    row = result.mappings().first()
    if row is None:  # pragma: no cover - INSERT ... RETURNING always yields a row
        raise RuntimeError("task INSERT RETURNING produced no row")
    return int(row["id"])
