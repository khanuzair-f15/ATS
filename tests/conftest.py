"""pytest configuration: offline fixture, markers, shared test utilities."""

from __future__ import annotations

import socket
import sys
from collections.abc import Generator

import pytest

# -----------------------------------------------------------------------------
# no_network: autouse fixture that blocks all outbound socket connections
# -----------------------------------------------------------------------------
#
# This fixture is autouse so every test runs offline by default. Tests that need
# a live database are marked with @pytest.mark.db and skip the guard.
#
# Implementation: subclass socket.socket and override connect/connect_ex/getaddrinfo
# to raise an AssertionError with a clear message. This is stricter than monkeypatch
# because it catches direct socket() calls too.
# -----------------------------------------------------------------------------


class _BlockingSocket(socket.socket):
    """socket.socket subclass that refuses to connect to non-localhost addresses.

    Allows localhost connections so the asyncio proactor event loop can create its
    internal socketpair on Windows.
    """

    def connect(self, address):  # type: ignore[override]
        host = address[0] if address else ""
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"network call blocked: socket.connect({address!r})")
        return super().connect(address)

    def connect_ex(self, address):  # type: ignore[override]
        host = address[0] if address else ""
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"network call blocked: socket.connect_ex({address!r})")
        return super().connect_ex(address)

    @staticmethod
    def getaddrinfo(*args, **kwargs):  # type: ignore[override]
        # Allow localhost resolution
        host = args[0] if args else ""
        if host not in ("127.0.0.1", "::1", "localhost", ""):
            raise AssertionError(f"network call blocked: socket.getaddrinfo({args!r})")
        return socket.getaddrinfo(*args, **kwargs)


@pytest.fixture(autouse=True)
def no_network(request: pytest.FixtureRequest) -> Generator[None, None, None]:
    """Fail any test that attempts a real socket connection, unless marked @pytest.mark.db."""
    if request.node.get_closest_marker("db"):
        yield
        return

    original_socket = socket.socket
    try:
        socket.socket = _BlockingSocket
        yield
    finally:
        socket.socket = original_socket


# -----------------------------------------------------------------------------
# Event loop policy for pytest-asyncio (avoids "no current event loop" on Windows)
# -----------------------------------------------------------------------------
@pytest.fixture(scope="session")
def event_loop_policy() -> None:
    if sys.platform == "win32":
        import asyncio

        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


# -----------------------------------------------------------------------------
# Shared FastAPI test client helper
# -----------------------------------------------------------------------------
@pytest.fixture
def app():
    from jobindex.api.app import create_app

    return create_app()


@pytest.fixture
async def async_client(app):
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client