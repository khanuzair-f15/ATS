"""Offline unit tests for the Phase 5A cloud execution worker.

These tests run without a live database or network: the async session is mocked,
so they exercise the claim / process / backoff / lease-expiry orchestration in
`jobindex.cloud.worker` using canned SQL results.
"""

from __future__ import annotations

from typing import Any

import pytest

from jobindex.cloud import queue
from jobindex.cloud.fixtures import fixture_payload
from jobindex.cloud.worker import (
    DEFAULT_BACKOFF_BASE_SECONDS,
    DEFAULT_BACKOFF_CAP_SECONDS,
    backoff_seconds,
    run_dispatch,
    tier_seconds_for,
)

# ---------------------------------------------------------------------------
# Fake result / session stand-ins
# ---------------------------------------------------------------------------


class _FakeMapping:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def keys(self) -> Any:
        return self._data.keys()

    def __iter__(self) -> Any:
        return iter(self._data)


class _FakeResult:
    def __init__(self, mappings=None, rowcount: int = 1) -> None:
        self._mappings = [_FakeMapping(m) for m in (mappings or [])]
        self.rowcount = rowcount

    def mappings(self) -> _FakeResult:
        return self

    def __iter__(self) -> Any:
        return iter(self._mappings)

    def first(self) -> _FakeMapping | None:
        return self._mappings[0] if self._mappings else None

    def scalar(self) -> Any:
        if self._mappings:
            return next(iter(self._mappings[0]._data.values()))
        return None


class _FakeSession:
    def __init__(self, claim_tasks: list[dict[str, Any]]) -> None:
        self.claim_tasks = [dict(t) for t in claim_tasks]
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.committed = False

    async def execute(self, sql: Any, params: Any = None) -> _FakeResult:
        self.executed.append((str(sql), params or {}))
        text = " ".join(str(sql).split())

        if "RETURNING q.id, q.source_id, q.run_id" in text:
            for t in self.claim_tasks:
                t["state"] = "running"
                t["attempts"] = int(t.get("attempts", 0)) + 1
                t["claim_token"] = f"token-{t['id']}"
            return _FakeResult(mappings=self.claim_tasks, rowcount=len(self.claim_tasks))

        if "RETURNING id, started_at" in text:
            return _FakeResult(mappings=[{"id": "run-1"}], rowcount=1)

        if "RETURNING id" in text and "fetch_run" in text:
            return _FakeResult(rowcount=1)

        if "RETURNING id, source_id, run_id" in text:
            return _FakeResult(mappings=[], rowcount=0)

        if "claim_token = :claim_token" in text:
            params_dict = params or {}
            task_id_raw = params_dict.get("task_id")
            claim_token_raw = params_dict.get("claim_token")
            task_id = int(task_id_raw) if task_id_raw is not None else 0
            claim_token = str(claim_token_raw) if claim_token_raw is not None else ""
            for t in self.claim_tasks:
                if int(t["id"]) == task_id and t.get("claim_token") == claim_token:
                    return _FakeResult(mappings=[{"id": task_id}], rowcount=1)
            return _FakeResult(rowcount=0)

        if "attempts < max_attempts" in text:
            task_id = (params or {}).get("task_id")
            for t in self.claim_tasks:
                if int(t["id"]) == int(task_id):
                    rescheduled = int(t["attempts"]) < int(t["max_attempts"])
                    if rescheduled:
                        return _FakeResult(mappings=[{"id": int(task_id)}], rowcount=1)
                    return _FakeResult(rowcount=0)
            return _FakeResult(rowcount=0)

        return _FakeResult(rowcount=1)

    async def commit(self) -> None:
        self.committed = True


def _make_tasks() -> list[dict[str, Any]]:
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
        },
    ]


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


class TestBackoffSeconds:
    def test_exponential_backoff(self) -> None:
        assert backoff_seconds(1, DEFAULT_BACKOFF_BASE_SECONDS, DEFAULT_BACKOFF_CAP_SECONDS) == 30
        assert backoff_seconds(2, DEFAULT_BACKOFF_BASE_SECONDS, DEFAULT_BACKOFF_CAP_SECONDS) == 60
        assert backoff_seconds(3, DEFAULT_BACKOFF_BASE_SECONDS, DEFAULT_BACKOFF_CAP_SECONDS) == 120
        assert backoff_seconds(4, DEFAULT_BACKOFF_BASE_SECONDS, DEFAULT_BACKOFF_CAP_SECONDS) == 240

    def test_cap(self) -> None:
        assert (
            backoff_seconds(10, DEFAULT_BACKOFF_BASE_SECONDS, DEFAULT_BACKOFF_CAP_SECONDS)
            == 3600
        )


class TestTierSecondsFor:
    def test_known_tiers(self) -> None:
        assert tier_seconds_for("hot") == 6 * 3600
        assert tier_seconds_for("warm") == 12 * 3600
        assert tier_seconds_for("cool") == 24 * 3600
        assert tier_seconds_for("dormant") == 72 * 3600

    def test_unknown_defaults_to_warm(self) -> None:
        assert tier_seconds_for("unknown") == tier_seconds_for("warm")


# ---------------------------------------------------------------------------
# run_dispatch orchestration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_dispatch_claims_and_processes_batch() -> None:
    session = _FakeSession(_make_tasks())
    result = await run_dispatch(session, worker_id="w1", batch_size=10)

    assert session.committed is True
    assert result["claimed"] == 2
    assert result["processed"] == 2
    assert result["succeeded"] == 1
    assert result["failed"] == 1

    # Claim used FOR UPDATE ... SKIP LOCKED
    executed_sql = " | ".join(s for s, _ in session.executed)
    assert "FOR UPDATE" in executed_sql
    assert "SKIP LOCKED" in executed_sql

    tasks = {t["task_id"]: t for t in result["tasks"]}
    assert tasks[101]["success"] is True
    assert tasks[101]["jobs_found"] == 5
    assert tasks[102]["success"] is False


@pytest.mark.asyncio
async def test_run_dispatch_returns_retry_count() -> None:
    session = _FakeSession(_make_tasks())
    result = await run_dispatch(session, worker_id="w1", batch_size=10)
    # task-b (flaky, fail_count=3, attempts=1 after claim) is retried once
    assert result["retried"] >= 1


@pytest.mark.asyncio
async def test_run_dispatch_exhausts_max_attempts() -> None:
    tasks = [
        {
            "id": 501,
            "source_id": "source-x",
            "run_id": "run-501",
            "task_kind": "refresh",
            "payload": fixture_payload("test-tenant-b", job_count=1, fail_count=9),
            "state": "pending",
            "attempts": 3,  # already at max_attempts
            "max_attempts": 3,
        }
    ]
    session = _FakeSession(tasks)
    result = await run_dispatch(session, worker_id="w1", batch_size=10)
    # task fails and is NOT retried (at max_attempts)
    assert result["failed"] == 1
    assert result["retried"] == 0


# ---------------------------------------------------------------------------
# queue-level helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_batch_increments_attempts_and_marks_running() -> None:
    session = _FakeSession(_make_tasks())
    claimed = await queue.claim_batch(session, worker_id="w1", batch_size=10)
    assert len(claimed) == 2
    for t in claimed:
        assert t["state"] == "running"
        assert t["attempts"] == 1


# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------


class TestFixturePayload:
    def test_defaults(self) -> None:
        payload = fixture_payload("test-tenant-a")
        assert payload["tenant_key"] == "test-tenant-a"
        assert payload["job_count"] == 5
        assert payload["fail_count"] == 0

    def test_flaky(self) -> None:
        payload = fixture_payload("test-tenant-b", job_count=2, fail_count=3)
        assert payload["fail_count"] == 3
        assert payload["job_count"] == 2
