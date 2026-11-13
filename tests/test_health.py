"""Health endpoint tests: /healthz and /readyz."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from jobindex.api.app import create_app


@pytest.mark.asyncio
async def test_healthz() -> None:
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "version" in data


@pytest.mark.asyncio
async def test_readyz_ok_when_db_probe_succeeds() -> None:
    """readyz returns 200 when the injected db probe succeeds."""
    async def ok_probe() -> None:
        return None

    app = create_app()
    app.dependency_overrides[app.dependency_overrides.get] = lambda _: ok_probe  # type: ignore

    # Simpler: directly override the dependency
    from jobindex.api.app import get_db_probe
    app.dependency_overrides[get_db_probe] = lambda: ok_probe

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/readyz")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    assert data["database"] == "ok"


@pytest.mark.asyncio
async def test_readyz_503_when_db_probe_fails() -> None:
    """readyz returns 503 when the injected db probe raises."""
    async def failing_probe() -> None:
        raise ConnectionError("simulated db down")

    from jobindex.api.app import create_app, get_db_probe
    app = create_app()
    app.dependency_overrides[get_db_probe] = lambda: failing_probe

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/readyz")
    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "not_ready"
    assert "detail" in data