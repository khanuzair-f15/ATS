"""Serverless worker orchestration.

This is the core of the Phase 5A execution path. A single invocation:

1. claims a bounded batch of pending tasks (``FOR UPDATE ... SKIP LOCKED``),
2. processes each task (synthetic extraction in Phase 5A; real connectors
   remain out of scope),
3. finalizes a ``fetch_run`` per task (written at claim/start, completed at end,
   ``abandoned`` on worker crash),
4. updates source health and scheduling,
5. retries failed tasks with exponential backoff (no duplicate processing),
6. reclaims leases from crashed workers.

All database access goes through `jobindex.cloud.queue`, so this module can be
tested offline with a mocked async session. No production connector code and no
78-platform inventory changes are introduced here.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from jobindex.cloud import queue

# ---------------------------------------------------------------------------
# Defaults (tunable via arguments; documented for the deployment requirements)
# ---------------------------------------------------------------------------

DEFAULT_BATCH_SIZE = 5
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE_SECONDS = 30
DEFAULT_BACKOFF_CAP_SECONDS = 3600  # 1 hour
DEFAULT_LEASE_TTL_SECONDS = 900  # 15 minutes (mirrors ADR 0003 reclaim)

# Refresh tier intervals in seconds (mirrors ADR 0003).
TIER_SECONDS = {
    "hot": 6 * 3600,
    "warm": 12 * 3600,
    "cool": 24 * 3600,
    "dormant": 72 * 3600,
}

# Extraction result keys produced by an extract_fn.
EXTRACTION_OUTCOME_SUCCESS = "success"
EXTRACTION_OUTCOME_FAILURE = "failure"
EXTRACTION_OUTCOME_PARTIAL = "partial"


def utcnow() -> datetime:
    return datetime.now(UTC)


def backoff_seconds(attempts: int, base: int, cap: int) -> int:
    """Exponential backoff with a cap: base * 2^(attempts-1), capped.

    ``attempts`` is the post-claim attempt counter (1-based). Deterministic — no
    jitter — so tests can assert exact retry times. Production may add jitter.
    """
    delay = base * (2 ** (attempts - 1))
    return int(min(delay, cap))


def tier_seconds_for(tier: str) -> int:
    return TIER_SECONDS.get(tier, TIER_SECONDS["warm"])


# ---------------------------------------------------------------------------
# Extraction interface
# ---------------------------------------------------------------------------

ExtractFn = Callable[[dict[str, Any]], dict[str, Any]]


def default_extract_fn(task: dict[str, Any]) -> dict[str, Any]:
    """Synthetic extraction used by the Phase 5A proof of concept.

    Reads a deterministic job list from the task payload. Returns a result dict
    describing the extraction outcome. Real connectors (list_page / fetch_detail)
    are intentionally not implemented in Phase 5A.
    """
    payload = task.get("payload") or {}
    fail_count = int(payload.get("fail_count", 0))
    attempt = int(task.get("attempts", 1))

    if fail_count > 0 and attempt <= fail_count:
        return {
            "outcome": EXTRACTION_OUTCOME_FAILURE,
            "error_class": "transient",
            "error_detail": "simulated transient upstream failure",
            "jobs_found": 0,
            "listing_complete": False,
            "http_status": 502,
            "duration_ms": 1200,
        }

    job_count = int(payload.get("job_count", 3))
    return {
        "outcome": EXTRACTION_OUTCOME_SUCCESS,
        "error_class": "none",
        "error_detail": None,
        "jobs_found": job_count,
        "listing_complete": True,
        "http_status": 200,
        "duration_ms": 450,
        "sample_jobs": [{"external_id": f"synthetic-{i}"} for i in range(job_count)],
    }


# ---------------------------------------------------------------------------
# Per-task processing
# ---------------------------------------------------------------------------


async def process_task(
    session: AsyncSession,
    task: dict[str, Any],
    worker_id: str,
    *,
    extract_fn: ExtractFn = default_extract_fn,
    now: Callable[[], datetime] = utcnow,
) -> dict[str, Any]:
    """Process one claimed task end to end inside the caller's transaction.

    Creates a ``fetch_run`` (started), runs the extraction, finalizes the
    ``fetch_run``, updates source health/scheduling, marks the task
    succeeded/failed, and appends execution-log events.
    """
    task_id: int = int(task["id"])
    source_id: str = str(task["source_id"])
    run_id: str = str(task["run_id"])
    claim_token: str = str(task["claim_token"])
    attempts: int = int(task["attempts"])

    started_at = now()
    started_at_monotonic = time.monotonic()
    fetch_run_id = await queue.create_fetch_run_started(
        session, source_id=source_id, queue_id=task_id, worker_id=worker_id
    )
    await queue.log_event(
        session,
        task_id=task_id,
        run_id=run_id,
        worker_id=worker_id,
        event="claimed",
        detail={"attempts": attempts, "started_at": started_at.isoformat()},
    )

    # Enforce a hard extraction deadline bounded by the lease TTL and the
    # Vercel function execution budget (10s on Hobby). The extraction must
    # complete well within the lease TTL so the heartbeat remains valid and
    # the function returns before the platform timeout.
    _EXTRACTION_DEADLINE_SECONDS = min(
        DEFAULT_LEASE_TTL_SECONDS / 2,  # 7.5s — well within lease TTL
        8.0,  # 8s — under Vercel Hobby's 10s function duration limit
    )

    try:
        extraction: dict[str, Any] = await asyncio.wait_for(
            asyncio.to_thread(extract_fn, task),
            timeout=_EXTRACTION_DEADLINE_SECONDS,
        )
    except TimeoutError:
        extraction = {
            "outcome": EXTRACTION_OUTCOME_FAILURE,
            "error_class": "transient",
            "error_detail": f"extraction deadline exceeded ({_EXTRACTION_DEADLINE_SECONDS}s)",
            "jobs_found": 0,
            "listing_complete": False,
            "http_status": 504,
            "duration_ms": int(_EXTRACTION_DEADLINE_SECONDS * 1000),
        }
    except Exception as exc:  # pragma: no cover - defensive; connectors raise
        extraction = {
            "outcome": EXTRACTION_OUTCOME_FAILURE,
            "error_class": "transient",
            "error_detail": f"extraction raised {type(exc).__name__}",
            "jobs_found": 0,
            "listing_complete": False,
            "http_status": None,
            "duration_ms": None,
        }

    # Refresh heartbeat after extraction before finalizing, so a slow but
    # healthy worker keeps its lease through the commit.
    await queue.refresh_heartbeat(session, task_id=task_id, claim_token=claim_token)

    duration_ms = int((time.monotonic() - started_at_monotonic) * 1000)
    outcome = extraction["outcome"]
    success = outcome == EXTRACTION_OUTCOME_SUCCESS
    error_class = extraction.get("error_class") or ("none" if success else "transient")
    http_status = extraction.get("http_status")

    # Finalize the fetch_run written at claim. Fenced by claim_token so a
    # stale/reclaimed worker cannot overwrite another worker's completed run.
    await queue.finalize_fetch_run(
        session,
        fetch_run_id=fetch_run_id,
        claim_token=claim_token,
        outcome=outcome,
        http_status=http_status,
        error_class=error_class,
        error_detail=extraction.get("error_detail"),
        jobs_found=int(extraction.get("jobs_found", 0)),
        listing_complete=bool(extraction.get("listing_complete", False)),
        duration_ms=duration_ms,
        worker_id=worker_id,
    )

    # Update source health. Fenced by claim_token so a stale/reclaimed worker
    # cannot corrupt source state after its lease expires.
    await queue.update_source_after_run(
        session,
        source_id,
        claim_token=claim_token,
        success=success,
        backoff_seconds=backoff_seconds(
            attempts, DEFAULT_BACKOFF_BASE_SECONDS, DEFAULT_BACKOFF_CAP_SECONDS
        ),
        tier_seconds=tier_seconds_for(payload_tier(task)),
    )

    if success:
        await queue.mark_task_succeeded(
            session,
            task_id,
            claim_token=claim_token,
            result={
                "jobs_found": int(extraction.get("jobs_found", 0)),
                "listing_complete": bool(extraction.get("listing_complete", False)),
                "duration_ms": duration_ms,
            },
        )
        await queue.log_event(
            session,
            task_id=task_id,
            run_id=run_id,
            worker_id=worker_id,
            event="succeeded",
            detail={"jobs_found": int(extraction.get("jobs_found", 0)), "duration_ms": duration_ms},
        )
    else:
        await queue.mark_task_failed(
            session,
            task_id,
            claim_token=claim_token,
            error_detail=extraction.get("error_detail"),
        )
        await queue.log_event(
            session,
            task_id=task_id,
            run_id=run_id,
            worker_id=worker_id,
            event="failed",
            detail={
                "error_class": error_class,
                "error_detail": extraction.get("error_detail"),
                "attempts": attempts,
                "max_attempts": int(task["max_attempts"]),
            },
        )

    return {
        "task_id": task_id,
        "source_id": source_id,
        "run_id": run_id,
        "outcome": outcome,
        "success": success,
        "jobs_found": int(extraction.get("jobs_found", 0)),
        "duration_ms": duration_ms,
    }


def payload_tier(task: dict[str, Any]) -> str:
    payload = task.get("payload") or {}
    return str(payload.get("tier", "warm"))


# ---------------------------------------------------------------------------
# Retry scheduling
# ---------------------------------------------------------------------------


async def schedule_retries(
    session: AsyncSession,
    failed_tasks: list[dict[str, Any]],
    *,
    base_seconds: int = DEFAULT_BACKOFF_BASE_SECONDS,
    cap_seconds: int = DEFAULT_BACKOFF_CAP_SECONDS,
) -> int:
    """Return failed tasks to pending with backoff, if attempts remain.

    Returns the number of tasks re-scheduled for retry.
    """
    retried = 0
    for task in failed_tasks:
        task_id = int(task["id"])
        attempts = int(task["attempts"])
        max_attempts = int(task["max_attempts"])
        if attempts >= max_attempts:
            await queue.log_event(
                session,
                task_id=task_id,
                run_id=str(task["run_id"]),
                worker_id="scheduler",
                event="exhausted",
                detail={"attempts": attempts, "max_attempts": max_attempts},
            )
            continue
        delay = backoff_seconds(attempts, base_seconds, cap_seconds)
        rescheduled = await queue.retry_task(session, task_id, delay)
        if rescheduled:
            await queue.log_event(
                session,
                task_id=task_id,
                run_id=str(task["run_id"]),
                worker_id="scheduler",
                event="retried",
                detail={"attempts": attempts, "next_claim_in_seconds": delay},
            )
            retried += 1
    return retried


# ---------------------------------------------------------------------------
# Top-level dispatch (the serverless function's work)
# ---------------------------------------------------------------------------


async def run_dispatch(
    session: AsyncSession,
    worker_id: str,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    extract_fn: ExtractFn = default_extract_fn,
    now: Callable[[], datetime] = utcnow,
    reclaim_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
) -> dict[str, Any]:
    """Claim and process one bounded batch of serverless extraction tasks.

    Runs in a single transaction so a function timeout before commit rolls
    everything back (tasks return to ``pending``). A hard kill leaves leases in
    ``running`` for ``reclaim_stale_tasks`` to recover on the next invocation.
    """
    claimed = await queue.claim_batch(session, worker_id=worker_id, batch_size=batch_size)

    processed: list[dict[str, Any]] = []
    failed_tasks: list[dict[str, Any]] = []

    for task in claimed:
        result = await process_task(
            session,
            task,
            worker_id,
            extract_fn=extract_fn,
            now=now,
        )
        processed.append(result)
        if not result["success"]:
            failed_tasks.append(task)

    reclaimed = await queue.reclaim_stale_tasks(session, lease_ttl_seconds=reclaim_ttl_seconds)
    for abandoned in reclaimed:
        await queue.abandon_fetch_run(
            session, task_id=int(abandoned["id"]), worker_id=worker_id
        )
        await queue.log_event(
            session,
            task_id=int(abandoned["id"]),
            run_id=str(abandoned["run_id"]),
            worker_id=worker_id,
            event="abandoned",
            detail={"reason": "worker heartbeat expired"},
        )

    retried = await schedule_retries(session, failed_tasks)

    await session.commit()

    return {
        "worker_id": worker_id,
        "claimed": len(claimed),
        "processed": len(processed),
        "succeeded": sum(1 for r in processed if result_success(r)),
        "failed": len(failed_tasks),
        "retried": retried,
        "abandoned": len(reclaimed),
        "tasks": processed,
    }


def result_success(result: dict[str, Any]) -> bool:
    return bool(result["success"])
