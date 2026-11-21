"""Synthetic fixtures for the Phase 5A proof of concept.

These represent two test tenants whose "extractions" are fully deterministic and
require no network access. They are used by `default_extract_fn` via the task
payload (`fixture` and `job_count`) and by the verification command to exercise
the claim / process / backoff path without any real connector or live tenant.

No production platform data and no 78-platform inventory entry is derived from
these fixtures.
"""

from __future__ import annotations

from typing import Any

# Deterministic synthetic tenants. Keys are synthetic `tenant_key` values; they
# are not platform keys and must not be confused with the 78 brief-exact platforms.
SYNTHETIC_TENANTS: dict[str, dict[str, Any]] = {
    "test-tenant-a": {
        "display_name": "Synthetic Tenant A",
        "family": "ats",
        "platform_hint": "greenhouse-like",
        "tier": "warm",
        "job_count": 5,
        "always_success": True,
    },
    "test-tenant-b": {
        "display_name": "Synthetic Tenant B (flaky)",
        "family": "ats",
        "platform_hint": "board-like",
        "tier": "cool",
        "job_count": 2,
        "always_success": False,
    },
}


def fixture_payload(
    tenant_key: str = "test-tenant-a",
    *,
    job_count: int | None = None,
    fail_count: int = 0,
    tier: str | None = None,
) -> dict[str, Any]:
    """Build a task ``payload`` for a synthetic tenant.

    ``fail_count`` makes `default_extract_fn` fail the first ``fail_count``
    attempts, which exercises retry-with-backoff without hitting a real upstream.
    """
    tenant = SYNTHETIC_TENANTS.get(tenant_key, SYNTHETIC_TENANTS["test-tenant-a"])
    return {
        "tenant_key": tenant_key,
        "fixture": tenant_key,
        "job_count": job_count if job_count is not None else tenant["job_count"],
        "tier": tier or tenant["tier"],
        "fail_count": fail_count,
        "display_name": tenant["display_name"],
    }
