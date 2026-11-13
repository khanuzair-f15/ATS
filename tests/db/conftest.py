"""Database test fixtures for @pytest.mark.db tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from jobindex.settings import get_settings


@pytest.fixture(scope="session")
def postgres_available() -> bool:
    """Check if PostgreSQL is available on localhost:5432 (session-scoped, runs once)."""
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        sock.connect(("localhost", 5432))
        sock.close()
        return True
    except (TimeoutError, ConnectionRefusedError, OSError):
        return False


@pytest.fixture(scope="function")
async def db_engine(request: pytest.FixtureRequest, postgres_available: bool):
    """Create a new async engine for each test function, skipping if DB unavailable.
    
    Only runs for tests marked with @pytest.mark.db.
    """
    import asyncio
    
    # Only run for tests marked with @pytest.mark.db
    if not request.node.get_closest_marker("db"):
        pytest.skip("Test not marked with @pytest.mark.db")
        return
    
    if not postgres_available:
        pytest.skip("PostgreSQL not running on localhost:5432")
        return
    
    settings = get_settings()
    engine = create_async_engine(
        settings.database_url,
        pool_pre_ping=False,
        pool_size=1,
        max_overflow=0,
        connect_args={"timeout": 3, "command_timeout": 3},
    )
    # Verify database is reachable before yielding
    try:
        async with asyncio.wait_for(engine.connect(), timeout=5) as conn:
            from sqlalchemy import text
            await conn.execute(text("SELECT 1"))
    except Exception as e:
        pytest.skip(f"Database not reachable: {e}")
        return
    try:
        yield engine
    finally:
        # Clean up
        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        loop.run_until_complete(engine.dispose())


@pytest.fixture(scope="function")
async def db_session(db_engine) -> AsyncGenerator:
    """Create a new session for each test function, rollback on completion."""
    
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    
    async with db_engine.connect() as conn:
        # Begin a transaction for this test
        trans = await conn.begin()
        try:
            session = async_session(bind=conn)
            try:
                yield session
            finally:
                await session.close()
        finally:
            # Rollback the transaction to isolate tests
            await trans.rollback()