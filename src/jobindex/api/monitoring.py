"""Source-health monitoring API.

Read-only, authenticated endpoints that report per-source (website) scraping
visibility and aggregate health. Uses only ``source``, ``fetch_run`` and
``serverless_task_queue`` as authoritative evidence; no new tables.

Two dimensions are tracked separately:

* Health status (``source.health``): healthy / degraded / open / half_open /
  disabled — reflects the circuit-breaker state of the source.
* Active execution status: whether a source currently has a claimed task with a
  valid lease heartbeat (state='running' and heartbeat not expired). Expired
  leases are NOT counted as active scraping. A failed scrape is NOT treated as
  proof that jobs have closed.

All endpoints require the ``JOBINDEX_CRON_SECRET`` bearer token. When the secret
is missing or empty authentication is fail-closed: every protected request is
rejected, never allowed through unauthenticated.
"""

from __future__ import annotations

import os
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jobindex.db.session import get_session

router = APIRouter()

# Lease TTL seconds used to decide whether a claimed task is still "active".
# Mirrors jobindex.cloud.worker.DEFAULT_LEASE_TTL_SECONDS (15 minutes).
LEASE_TTL_SECONDS = 900


# ---------------------------------------------------------------------------
# Authentication (fail-closed)
# ---------------------------------------------------------------------------


def _cron_secret() -> str | None:
    return os.environ.get("JOBINDEX_CRON_SECRET")


def _extract_bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization")
    if not auth:
        return None
    if not auth.lower().startswith("bearer "):
        return None
    return auth[7:].strip()


async def require_cron_secret(request: Request) -> None:
    """Require a valid cron-secret bearer token.

    Fail-closed: if ``JOBINDEX_CRON_SECRET`` is missing or empty, or the
    presented bearer token does not match, the request is rejected. Authentication
    is never silently disabled.
    """
    secret = _cron_secret()
    if not secret or not secret.strip():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="monitoring authentication is not configured",
        )
    token = _extract_bearer_token(request)
    if token is None or token != secret.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid cron secret",
        )


# ---------------------------------------------------------------------------
# Source health row
# ---------------------------------------------------------------------------


async def _fetch_source_rows(
    session: AsyncSession,
) -> list[dict[str, Any]]:
    """Return every source enriched with health, active status and latest run."""
    # Latest fetch run per source (LATERAL) and whether a task is actively
    # claimed with a valid heartbeat.
    sql = text(
        """
        SELECT
            s.id,
            s.platform_key,
            s.tenant_key,
            s.display_name,
            s.entry_url,
            s.enabled,
            s.health,
            s.consecutive_failures,
            s.last_success_at,
            s.last_attempt_at,
            s.next_due_at,
            s.refresh_tier,
            s.rate_limit_rps,
            p.display_name AS platform_display_name,
            p.family,
            p."class" AS platform_class,
            (
                SELECT EXISTS (
                    SELECT 1
                    FROM serverless_task_queue q
                    WHERE q.source_id = s.id
                      AND q.state = 'running'
                      AND q.heartbeat_at >= now() - (:lease_ttl * interval '1 second')
                )
            ) AS actively_scraping,
            (
                SELECT EXISTS (
                    SELECT 1
                    FROM serverless_task_queue q
                    WHERE q.source_id = s.id AND q.state = 'running'
                )
            ) AS claimed,
            fr.outcome AS latest_outcome,
            fr.started_at AS latest_started_at,
            fr.finished_at AS latest_finished_at,
            fr.jobs_found AS latest_jobs_found,
            fr.error_class AS latest_error_class,
            fr.listing_complete AS latest_listing_complete
        FROM source s
        JOIN platform p ON p.key = s.platform_key
        LEFT JOIN LATERAL (
            SELECT fr2.outcome, fr2.started_at, fr2.finished_at,
                   fr2.jobs_found, fr2.error_class, fr2.listing_complete
            FROM fetch_run fr2
            WHERE fr2.source_id = s.id
            ORDER BY fr2.started_at DESC
            LIMIT 1
        ) fr ON TRUE
        ORDER BY s.platform_key, s.tenant_key
        """
    )
    result = await session.execute(sql, {"lease_ttl": LEASE_TTL_SECONDS})
    return [dict(row) for row in result.mappings()]


def _iso_z(value: Any) -> str | None:
    if value is None:
        return None
    from datetime import UTC, datetime

    if hasattr(value, "tzinfo") and value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    return str(value)


def _source_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_id": str(row["id"]),
        "platform_key": row["platform_key"],
        "platform_display_name": row["platform_display_name"],
        "family": row["family"],
        "platform_class": row["platform_class"],
        "tenant_key": row["tenant_key"],
        "display_name": row["display_name"],
        "entry_url": row["entry_url"],
        "enabled": bool(row["enabled"]),
        "health": row["health"],
        "actively_scraping": bool(row["actively_scraping"]),
        "claimed": bool(row["claimed"]),
        "consecutive_failures": int(row["consecutive_failures"] or 0),
        "latest_outcome": row["latest_outcome"],
        "latest_error_class": row["latest_error_class"],
        "latest_jobs_found": int(row["latest_jobs_found"] or 0),
        "latest_listing_complete": bool(row["latest_listing_complete"]),
        "last_attempt_at": _iso_z(row["last_attempt_at"]),
        "last_success_at": _iso_z(row["last_success_at"]),
        "next_due_at": _iso_z(row["next_due_at"]),
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/api/monitoring/summary")
async def monitoring_summary(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    """Aggregate source-health counts."""
    await require_cron_secret(request)
    rows = await _fetch_source_rows(session)

    total = len(rows)
    enabled = sum(1 for r in rows if r["enabled"])
    actively_scraping = sum(1 for r in rows if r["actively_scraping"])
    claimed = sum(1 for r in rows if r["claimed"])
    healthy = sum(1 for r in rows if r["health"] == "healthy")
    degraded = sum(1 for r in rows if r["health"] == "degraded")
    unavailable = sum(1 for r in rows if r["health"] in ("open", "disabled", "half_open"))
    paused = sum(1 for r in rows if not r["enabled"])
    blocked = sum(1 for r in rows if r["latest_error_class"] == "blocked")
    pending_tasks = sum(1 for r in rows if not r["claimed"] and r["enabled"])

    return {
        "total_discovered": total,
        "enabled": enabled,
        "actively_scraping": actively_scraping,
        "claimed": claimed,
        "healthy": healthy,
        "degraded": degraded,
        "unavailable": unavailable,
        "paused": paused,
        "blocked": blocked,
        "pending_tasks": pending_tasks,
    }


@router.get("/api/monitoring/sources")
async def monitoring_sources(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    """List every source (website) with its health and active-execution status."""
    await require_cron_secret(request)
    rows = await _fetch_source_rows(session)
    return {"sources": [_source_summary(r) for r in rows]}


@router.get("/api/monitoring/sources/{source_id}")
async def monitoring_source_detail(
    source_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    """Detail for one source (website)."""
    await require_cron_secret(request)
    rows = await _fetch_source_rows(session)
    for row in rows:
        if str(row["id"]) == source_id:
            return _source_summary(row)
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source not found")


@router.get("/api/monitoring/platforms")
async def monitoring_platforms(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    """Source counts grouped by platform."""
    await require_cron_secret(request)
    rows = await _fetch_source_rows(session)
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row["platform_key"]
        entry = grouped.setdefault(
            key,
            {
                "platform_key": key,
                "platform_display_name": row["platform_display_name"],
                "family": row["family"],
                "platform_class": row["platform_class"],
                "sources": 0,
                "healthy": 0,
                "degraded": 0,
                "unavailable": 0,
                "actively_scraping": 0,
                "blocked": 0,
            },
        )
        entry["sources"] += 1
        if row["health"] == "healthy":
            entry["healthy"] += 1
        elif row["health"] == "degraded":
            entry["degraded"] += 1
        elif row["health"] in ("open", "disabled", "half_open"):
            entry["unavailable"] += 1
        if row["actively_scraping"]:
            entry["actively_scraping"] += 1
        if row["latest_error_class"] == "blocked":
            entry["blocked"] += 1
    return {"platforms": list(grouped.values())}


# ---------------------------------------------------------------------------
# Worker dispatch endpoint (POST /api/extract)
# ---------------------------------------------------------------------------


@router.post("/api/extract")
async def run_extract(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    """Claim and process a bounded batch of extraction tasks.

    Triggered by Supabase Cron via pg_net, or directly for review. Protected by
    the cron secret; fail-closed when the secret is not configured.
    """
    await require_cron_secret(request)

    from jobindex.cloud.worker import DEFAULT_BATCH_SIZE, run_dispatch

    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        body = {}

    worker_id = str(
        body.get("worker_id") or os.environ.get("WORKER_ID", f"vercel-{uuid.uuid4().hex[:8]}")
    )
    try:
        batch_size = int(body.get("batch_size", DEFAULT_BATCH_SIZE))
    except (TypeError, ValueError):
        batch_size = DEFAULT_BATCH_SIZE
    batch_size = max(1, min(batch_size, 100))

    result = await run_dispatch(session, worker_id=worker_id, batch_size=batch_size)
    return result
