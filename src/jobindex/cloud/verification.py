"""Phase 5A cloud verification logic (reusable by CLI and scripts/verify_cloud.py).

Runs the offline verification of the cloud-only execution prototype without a
live database or a deployed Vercel/Supabase project. See scripts/verify_cloud.py
for the command-line entry point.

This module uses intentionally simple fakes (no real database) so it can run
offline. It is type-annotated to satisfy the project's strict mypy configuration.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

ROOT = Path(__file__).resolve().parents[3]

REQUIRED_FILES: list[str] = [
    "migrations/versions/0004_serverless_execution.py",
    "src/jobindex/cloud/__init__.py",
    "src/jobindex/cloud/queue.py",
    "src/jobindex/cloud/worker.py",
    "src/jobindex/cloud/fixtures.py",
    "src/jobindex/cloud/verification.py",
    "api/extract.py",
    "vercel.json",
    "supabase/cron.sql",
]

CheckResult = dict[str, Any]


class FakeMapping:
    """A minimal SQLAlchemy RowMapping stand-in."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def keys(self) -> list[str]:
        return list(self._data.keys())

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def __iter__(self) -> Any:
        return iter(self._data)


class FakeResult:
    """A minimal SQLAlchemy Result stand-in."""

    def __init__(
        self,
        mappings: list[FakeMapping] | None = None,
        rowcount: int = 1,
        scalar_value: Any = None,
    ) -> None:
        self._mappings = list(mappings or [])
        self.rowcount = rowcount
        self._scalar_value = scalar_value

    def mappings(self) -> FakeResult:
        return self

    def __iter__(self) -> Any:
        return iter(self._mappings)

    def first(self) -> FakeMapping | None:
        return self._mappings[0] if self._mappings else None

    def scalar(self) -> Any:
        if self._scalar_value is not None:
            return self._scalar_value
        if self._mappings:
            return next(iter(self._mappings[0]._data.values()))
        return None


class FakeSession:
    """Mock async session that records calls and returns canned queue rows."""

    def __init__(self, tasks: list[dict[str, Any]]) -> None:
        self.tasks = [dict(t) for t in tasks]
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.committed = False
        self.claim_results = [dict(t) for t in tasks]

    async def execute(self, sql: Any, params: Any = None) -> FakeResult:
        self.executed.append((str(sql), params or {}))
        text_sql = " ".join(str(sql).split())

        if "RETURNING q.id, q.source_id, q.run_id" in text_sql:
            import uuid as _uuid

            for t in self.claim_results:
                t["state"] = "running"
                t["attempts"] = int(t.get("attempts", 0)) + 1
                t["claim_token"] = str(_uuid.uuid4())
            return FakeResult(
                mappings=[FakeMapping(t) for t in self.claim_results],
                rowcount=len(self.claim_results),
            )

        if "RETURNING id, started_at" in text_sql:
            return FakeResult(
                mappings=[FakeMapping({"id": "run-1"})], rowcount=1
            )

        if "RETURNING id" in text_sql and "fetch_run" in text_sql:
            return FakeResult(rowcount=1)

        if "RETURNING id, source_id, run_id" in text_sql:
            return FakeResult(mappings=[], rowcount=0)

        if "claim_token = :claim_token" in text_sql:
            params_dict = params or {}
            task_id_raw = params_dict.get("task_id")
            claim_token_raw = params_dict.get("claim_token")
            task_id = cast(int, task_id_raw) if task_id_raw is not None else 0
            claim_token = str(claim_token_raw) if claim_token_raw is not None else ""
            for t in self.tasks:
                if int(t["id"]) == task_id and t.get("claim_token") == claim_token:
                    return FakeResult(
                        mappings=[FakeMapping({"id": task_id})], rowcount=1
                    )
            return FakeResult(rowcount=0)

        if "WHERE id = :task_id AND state = 'failed'" in text_sql:
            params_dict = params or {}
            task_id_raw = params_dict.get("task_id")
            task_id = cast(int, task_id_raw) if task_id_raw is not None else 0
            for t in self.tasks:
                if int(t["id"]) == task_id:
                    rescheduled = int(t["attempts"]) < int(t["max_attempts"])
                    if rescheduled:
                        return FakeResult(
                            mappings=[FakeMapping({"id": task_id})], rowcount=1
                        )
                    return FakeResult(rowcount=0)
            return FakeResult(rowcount=0)

        return FakeResult(rowcount=1)

    async def commit(self) -> None:
        self.committed = True

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False


def make_tasks() -> list[dict[str, Any]]:
    from jobindex.cloud.fixtures import fixture_payload

    return [
        {
            "id": 101,
            "source_id": "source-a",
            "run_id": "run-101",
            "task_kind": "refresh",
            "payload": fixture_payload("test-tenant-a", job_count=5),
            "state": "pending",
            "attempts": 0,
            "max_attempts": 3,
            "next_claim_at": datetime.now(UTC),
        },
        {
            "id": 102,
            "source_id": "source-b",
            "run_id": "run-102",
            "task_kind": "refresh",
            "payload": fixture_payload("test-tenant-b", job_count=2, fail_count=3),
            "state": "pending",
            "attempts": 0,
            "max_attempts": 3,
            "next_claim_at": datetime.now(UTC),
        },
    ]


async def _run_dispatch_simulation() -> list[CheckResult]:
    from jobindex.cloud.worker import run_dispatch

    tasks = make_tasks()
    session = FakeSession(tasks)
    result = await run_dispatch(
        cast(AsyncSession, session), worker_id="verify-worker-1", batch_size=10
    )

    checks: list[CheckResult] = []
    checks.append({"name": "run_dispatch commits transaction", "ok": session.committed})
    checks.append(
        {
            "name": "claim uses FOR UPDATE ... SKIP LOCKED",
            "ok": any(
                "FOR UPDATE" in s and "SKIP LOCKED" in s for s, _ in session.executed
            ),
        }
    )
    checks.append(
        {
            "name": "claim + process both tasks",
            "ok": result.get("claimed") == 2 and result.get("processed") == 2,
            "detail": f"claimed={result.get('claimed')} processed={result.get('processed')}",
        }
    )
    checks.append(
        {
            "name": "one task succeeded, one failed",
            "ok": result.get("succeeded") == 1 and result.get("failed") == 1,
            "detail": f"succeeded={result.get('succeeded')} failed={result.get('failed')}",
        }
    )
    checks.append(
        {
            "name": "failed task retried with backoff",
            "ok": result.get("retried", 0) >= 1,
        }
    )
    tasks_out = result.get("tasks", [])
    tenant_a = tasks_out[0] if tasks_out else None
    checks.append(
        {
            "name": "succeeded task reports jobs_found=5",
            "ok": tenant_a is not None
            and tenant_a["success"]
            and tenant_a["jobs_found"] == 5,
        }
    )
    tenant_b = tasks_out[1] if len(tasks_out) > 1 else None
    checks.append(
        {
            "name": "failed task reports failure",
            "ok": tenant_b is not None and not tenant_b["success"],
        }
    )
    log_events = [
        p
        for _, p in session.executed
        if "serverless_execution_log" in _
    ]
    checks.append(
        {"name": "execution log events recorded", "ok": len(log_events) >= 3}
    )
    return checks


class _RetryExhaustionSession:
    async def execute(self, sql: Any, params: Any = None) -> FakeResult:
        text_sql = str(sql)
        if "attempts < max_attempts" in text_sql:
            return FakeResult(rowcount=0)
        return FakeResult(rowcount=1)

    async def commit(self) -> None:
        pass

    async def __aenter__(self) -> _RetryExhaustionSession:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False


async def _retry_exhaustion() -> CheckResult:
    from jobindex.cloud.worker import schedule_retries

    session = _RetryExhaustionSession()
    retried = await schedule_retries(
        cast(AsyncSession, session),
        [{"id": 999, "run_id": "run-999", "attempts": 3, "max_attempts": 3}],
    )
    return {"name": "task at max_attempts is exhausted (not retried)", "ok": retried == 0}


class _LeaseExpirySession:
    async def execute(self, sql: Any, params: Any = None) -> FakeResult:
        if "heartbeat_at < now()" in str(sql):
            return FakeResult(
                mappings=[
                    FakeMapping(
                        {
                            "id": 7,
                            "source_id": "source-x",
                            "run_id": "run-7",
                        }
                    )
                ],
                rowcount=1,
            )
        return FakeResult(rowcount=1)

    async def commit(self) -> None:
        pass

    async def __aenter__(self) -> _LeaseExpirySession:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False


async def _lease_expiry() -> CheckResult:
    from jobindex.cloud.queue import reclaim_stale_tasks

    session = _LeaseExpirySession()
    reclaimed = await reclaim_stale_tasks(
        cast(AsyncSession, session), lease_ttl_seconds=900
    )
    return {
        "name": "expired lease reclaimed as abandoned",
        "ok": len(reclaimed) == 1 and reclaimed[0]["id"] == 7,
    }


def _check_files() -> list[CheckResult]:
    out: list[CheckResult] = []
    ok_all = True
    for rel in REQUIRED_FILES:
        path = ROOT / rel
        ok = path.is_file()
        out.append(
            {
                "name": f"file exists: {rel}",
                "ok": ok,
                "detail": "" if ok else "missing",
            }
        )
        ok_all = ok_all and ok
    out.insert(0, {"name": "all required files present", "ok": ok_all})
    return out


def _check_imports() -> list[CheckResult]:
    out: list[CheckResult] = []
    ok_all = True
    for mod in [
        "jobindex.cloud",
        "jobindex.cloud.queue",
        "jobindex.cloud.worker",
        "jobindex.cloud.fixtures",
    ]:
        try:
            __import__(mod)
            out.append({"name": f"import: {mod}", "ok": True})
        except Exception as exc:
            out.append(
                {"name": f"import: {mod}", "ok": False, "detail": repr(exc)}
            )
            ok_all = False
    out.insert(0, {"name": "all modules import", "ok": ok_all})
    return out


def _check_pure_functions() -> list[CheckResult]:
    from jobindex.cloud.worker import backoff_seconds, tier_seconds_for

    try:
        assert backoff_seconds(1, 30, 3600) == 30
        assert backoff_seconds(2, 30, 3600) == 60
        assert backoff_seconds(3, 30, 3600) == 120
        assert backoff_seconds(4, 30, 3600) == 240
        assert backoff_seconds(10, 30, 3600) == 3600
        assert tier_seconds_for("hot") == 6 * 3600
        assert tier_seconds_for("warm") == 12 * 3600
        assert tier_seconds_for("cool") == 24 * 3600
        assert tier_seconds_for("dormant") == 72 * 3600
        assert tier_seconds_for("unknown") == tier_seconds_for("warm")
        return [
            {
                "name": "pure functions (backoff + tiers) deterministic",
                "ok": True,
            }
        ]
    except Exception as exc:
        return [
            {
                "name": "pure functions (backoff + tiers) deterministic",
                "ok": False,
                "detail": repr(exc),
            }
        ]


async def run_checks() -> list[CheckResult]:
    checks: list[CheckResult] = []
    checks.extend(_check_files())
    checks.extend(_check_imports())
    checks.extend(_check_pure_functions())
    sim = await _run_dispatch_simulation()
    checks.extend(sim)
    checks.append(await _retry_exhaustion())
    checks.append(await _lease_expiry())
    return checks


def run_verification(json_output: bool = False) -> tuple[bool, list[CheckResult]]:
    """Run all offline checks. Returns (all_ok, results)."""
    results = asyncio.run(run_checks())
    passed = sum(1 for r in results if r["ok"])
    failed = len(results) - passed
    if json_output:
        print(json.dumps({"passed": passed, "failed": failed, "checks": results}, indent=2))
    else:
        print("Phase 5A cloud verification (offline, no live DB/Vercel/Supabase)")
        print("=" * 65)
        for r in results:
            status = "PASS" if r["ok"] else "FAIL"
            line = f"[{status}] {r['name']}"
            if r.get("detail"):
                line += f" — {r['detail']}"
            print(line)
        print("=" * 65)
        print(f"Results: {passed} passed, {failed} failed out of {len(results)} checks")
    all_ok = failed == 0
    return all_ok, results
