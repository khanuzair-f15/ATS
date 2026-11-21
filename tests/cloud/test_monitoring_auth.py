"""Offline tests for source-health monitoring authentication.

These tests run without a database or network: they exercise
``jobindex.api.monitoring.require_cron_secret`` directly against a fake request
with a monkeypatched environment. Authentication is fail-closed: a missing or
empty ``JOBINDEX_CRON_SECRET`` must never disable authentication.
"""

from __future__ import annotations

import pytest

from jobindex.api.monitoring import require_cron_secret


class _FakeRequest:
    """Minimal request stand-in exposing ``headers``."""

    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = dict(headers or {})


async def test_missing_cron_secret_rejects_all_requests(monkeypatch) -> None:
    """Missing JOBINDEX_CRON_SECRET is fail-closed: every request rejected."""
    monkeypatch.delenv("JOBINDEX_CRON_SECRET", raising=False)
    request = _FakeRequest({"authorization": "Bearer any-token"})
    with pytest.raises(Exception) as excinfo:
        await require_cron_secret(request)
    assert excinfo.value.status_code == 503


async def test_empty_cron_secret_rejects_all_requests(monkeypatch) -> None:
    """Empty JOBINDEX_CRON_SECRET is fail-closed: every request rejected."""
    monkeypatch.setenv("JOBINDEX_CRON_SECRET", "")
    request = _FakeRequest({"authorization": "Bearer any-token"})
    with pytest.raises(Exception) as excinfo:
        await require_cron_secret(request)
    assert excinfo.value.status_code == 503


async def test_valid_secret_accepts_request(monkeypatch) -> None:
    """A matching bearer token is accepted when the secret is configured."""
    monkeypatch.setenv("JOBINDEX_CRON_SECRET", "super-secret")
    request = _FakeRequest({"authorization": "Bearer super-secret"})
    await require_cron_secret(request)  # should not raise


async def test_wrong_secret_rejected(monkeypatch) -> None:
    """A non-matching bearer token is rejected with 401."""
    monkeypatch.setenv("JOBINDEX_CRON_SECRET", "super-secret")
    request = _FakeRequest({"authorization": "Bearer wrong-token"})
    with pytest.raises(Exception) as excinfo:
        await require_cron_secret(request)
    assert excinfo.value.status_code == 401


async def test_missing_authorization_header_rejected(monkeypatch) -> None:
    """No Authorization header is rejected with 401."""
    monkeypatch.setenv("JOBINDEX_CRON_SECRET", "super-secret")
    request = _FakeRequest({})
    with pytest.raises(Exception) as excinfo:
        await require_cron_secret(request)
    assert excinfo.value.status_code == 401


async def test_non_bearer_auth_rejected(monkeypatch) -> None:
    """Non-bearer Authorization schemes are rejected with 401."""
    monkeypatch.setenv("JOBINDEX_CRON_SECRET", "super-secret")
    request = _FakeRequest({"authorization": "Basic abc123"})
    with pytest.raises(Exception) as excinfo:
        await require_cron_secret(request)
    assert excinfo.value.status_code == 401


def test_require_cron_secret_is_async() -> None:
    """require_cron_secret returns an awaitable (async dependency)."""
    import asyncio

    assert asyncio.iscoroutinefunction(require_cron_secret)
