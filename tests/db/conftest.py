"""Database test fixtures for @pytest.mark.db tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

if TYPE_CHECKING:
    from jobindex.settings import get_settings
else:
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
async def db_engine(
    request: pytest.FixtureRequest, postgres_available: bool
) -> AsyncGenerator[AsyncEngine, None]:
    """Create a new async engine for each test function.
    
    Only runs for tests marked with @pytest.mark.db.
    Fails hard if PostgreSQL is expected (CI) but unreachable.
    
    Pool size is 2 (not 1) because some tests require TWO concurrent connections:
    - One for savepoint-based isolation (db_session via test_source fixture)
    - One for real transaction commit to fire deferred triggers (db_session_commit)
    
    This is not a workaround for connection leaks; it is a legitimate requirement
    for tests that need both savepoint isolation (for fixture setup) and genuine
    transaction commit (to fire PostgreSQL deferred constraint triggers).
    """
    import asyncio
    import os
    
    # Only run for tests marked with @pytest.mark.db
    if not request.node.get_closest_marker("db"):
        pytest.skip("Test not marked with @pytest.mark.db")
        return
    
    if not postgres_available:
        # PostgreSQL is not available (port closed or unreachable).
        # In CI this is a configuration error; locally we skip.
        if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
            pytest.fail("PostgreSQL is not available on localhost:5432 (expected in CI).")
        else:
            pytest.skip("PostgreSQL not running on localhost:5432")
        return
    
    settings = get_settings()
    engine = create_async_engine(
        settings.database_url,
        pool_pre_ping=False,
        pool_size=2,  # See docstring: two concurrent connections needed per test
        max_overflow=0,
        connect_args={"timeout": 3, "command_timeout": 3},
    )
    # Verify database is reachable before yielding
    try:
        async with asyncio.timeout(5):
            async with engine.connect() as conn:
                from sqlalchemy import text
                await conn.execute(text("SELECT 1"))
    except Exception as e:
        pytest.fail(f"Database not reachable after port check: {e}")
        return
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture(scope="function")
async def db_session(db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Create a session for normal tests using savepoint-based isolation.
    
    Uses join_transaction_mode="create_savepoint" to join an outer transaction.
    The outer transaction is always rolled back, providing test isolation.
    Session commits release the savepoint but do not commit the outer transaction.
    """
    async_session = async_sessionmaker(
        db_engine,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    
    async with db_engine.connect() as conn:
        # Begin outer transaction that will be rolled back
        outer_trans = await conn.begin()
        try:
            session = async_session(bind=conn)
            try:
                yield session
            finally:
                await session.close()
        finally:
            # Always rollback the outer transaction for isolation
            await outer_trans.rollback()


@pytest.fixture(scope="function")
async def db_session_commit(db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Create a session for tests requiring real transaction commit.
    
    Used by deferred-trigger tests (liveness, first_seen_at) where the test
    must commit the actual transaction to fire deferred triggers.
    The session manages its own transaction; fixture rolls back on cleanup
    if the test's commit failed (PostgreSQL auto-rolls back on error).
    """
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    
    async with db_engine.connect() as conn:
        session = async_session(bind=conn)
        try:
            yield session
        except Exception:
            # If test raised an exception (e.g., IntegrityError), PostgreSQL
            # has already rolled back the transaction. We still need to close
            # the session to return the connection to the pool.
            await session.close()
            raise
        else:
            # Test completed without exception. If session has an active transaction
            # (test didn't commit), roll it back. If test committed, the transaction
            # is already committed and we just close the session.
            if session.in_transaction():
                await session.rollback()
            await session.close()