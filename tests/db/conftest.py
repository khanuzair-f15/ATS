"""Database test fixtures for @pytest.mark.db tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jobindex.db import (
    Job,
    JobCluster,
    JobOccurrence,
    Platform,
    Source,
)
from jobindex.db.enums import (
    CoverageState,
    PlatformClass,
    TenantStatus,
    TransportClass,
)
from jobindex.settings import get_settings


@pytest.fixture(scope="session")
def postgres_available() -> bool:
    """Check if PostgreSQL is available at the configured endpoint (session-scoped, runs once)."""
    import socket

    from sqlalchemy.engine import make_url

    url = make_url(get_settings().database_url)
    try:
        with socket.create_connection((url.host or "localhost", url.port or 5432), timeout=0.5):
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
    
    Real-commit teardown may use a second connection for independent cleanup.
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
            pytest.fail("PostgreSQL is not available at the configured endpoint (expected in CI).")
        else:
            pytest.skip("PostgreSQL not running at the configured endpoint")
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
    """Use a real database transaction so deferred triggers run at commit."""
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as session:
        yield session


@dataclass(frozen=True)
class CommittedRecords:
    """The primary keys reserved for one real-commit test."""

    source_id: UUID = field(default_factory=uuid4)
    job_id: UUID = field(default_factory=uuid4)
    cluster_id: UUID = field(default_factory=uuid4)
    occurrence_id: UUID = field(default_factory=uuid4)


@pytest.fixture
async def committed_records(
    db_session_commit: AsyncSession,
    db_engine: AsyncEngine,
) -> AsyncGenerator[CommittedRecords, None]:
    records = CommittedRecords()
    platform_key = f"commit_test_{records.source_id.hex}"
    platform = Platform(
        key=platform_key,
        display_name="Commit Test Platform",
        family="ats",
        platform_class=PlatformClass.C1,
        wave="S3",
        transport=TransportClass.API_JSON,
        auth_kind="none",
        coverage_state=CoverageState.OUTSTANDING,
        coverage_reason="test",
        notes="test",
    )
    source = Source(
        id=records.source_id,
        platform_key=platform_key,
        tenant_key="commit_test_tenant",
        display_name="Commit Test Tenant",
        entry_url="https://commit.example.com/careers",
        transport_class=TransportClass.API_JSON,
        connector=platform_key,
        tenant_status=TenantStatus.VALIDATED,
    )
    db_session_commit.add(platform)
    await db_session_commit.flush()
    db_session_commit.add(source)
    await db_session_commit.flush()
    try:
        await db_session_commit.commit()
        yield records
    finally:
        await db_session_commit.rollback()
        async with db_engine.begin() as conn:
            # Lock both FK targets before checking for incoming SET NULL links.
            # Other incoming links use RESTRICT or NO ACTION and fail atomically.
            job = (Job.id == records.job_id) & (Job.origin_source_id == records.source_id)
            cluster = (JobCluster.id == records.cluster_id) & (
                JobCluster.canonical_job_id == records.job_id
            )
            await conn.execute(select(Job.id).where(job).with_for_update())
            await conn.execute(select(JobCluster.id).where(cluster).with_for_update())
            shared = await conn.scalar(
                select(Job.id)
                .where(
                    Job.id != records.job_id,
                    or_(
                        Job.cluster_id == records.cluster_id,
                        Job.merged_into_job_id == records.job_id,
                    ),
                )
                .limit(1)
            )
            if shared is not None:
                raise RuntimeError(f"Unrelated job {shared} references fixture records")

            await conn.execute(
                delete(JobOccurrence).where(
                    JobOccurrence.id == records.occurrence_id,
                    JobOccurrence.source_id == records.source_id,
                    JobOccurrence.job_id == records.job_id,
                )
            )
            await conn.execute(delete(JobCluster).where(cluster))
            await conn.execute(delete(Job).where(job))
            await conn.execute(
                delete(Source).where(
                    Source.id == records.source_id,
                    Source.platform_key == platform_key,
                )
            )
            await conn.execute(delete(Platform).where(Platform.key == platform_key))
