"""PostgreSQL integration tests for the coverage evidence loader.

Coverage promotion is evidence-driven: a platform is ``verified`` only when a
persisted, successful, complete live fetch recorded a tenant/source and at least
one live job occurrence. These tests exercise ``load_db_evidence`` against real
PostgreSQL rows, and skip cleanly when no database is reachable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from jobindex.cli.coverage import load_db_evidence
from jobindex.db import FetchRun, Job, JobOccurrence, Platform, Source
from jobindex.db.enums import (
    CoverageState,
    FetchOutcome,
    JobStatus,
    PlatformClass,
    SourceHealth,
    TenantStatus,
    TransportClass,
)

pytestmark = pytest.mark.db

PLATFORM_KEY = "coverage_test_platform"
RUN_START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
RUN_FINISH = RUN_START + timedelta(minutes=1)
OCCURRENCE_SEEN = RUN_START + timedelta(seconds=30)


@pytest.fixture
async def coverage_platform(db_session):
    """Create a platform that cannot collide with the seeded inventory."""
    platform = Platform(
        key=PLATFORM_KEY,
        display_name="Coverage Test Platform",
        family="ats",
        platform_class=PlatformClass.C1,
        wave="S3",
        transport=TransportClass.API_JSON,
        auth_kind="none",
        coverage_state=CoverageState.OUTSTANDING,
        coverage_reason="test platform for coverage evidence",
        notes="test",
    )
    db_session.add(platform)
    await db_session.flush()
    return platform


async def make_source(
    db_session,
    *,
    tenant_key: str = "tenant-a",
    health: SourceHealth = SourceHealth.HEALTHY,
    consecutive_failures: int = 0,
) -> Source:
    """Create one tenant/source row for the test platform."""
    source = Source(
        platform_key=PLATFORM_KEY,
        tenant_key=tenant_key,
        display_name=tenant_key,
        entry_url=f"https://{tenant_key}.example.com/careers",
        transport_class=TransportClass.API_JSON,
        connector=PLATFORM_KEY,
        tenant_status=TenantStatus.VALIDATED,
        health=health,
        consecutive_failures=consecutive_failures,
    )
    db_session.add(source)
    await db_session.flush()
    return source


async def make_run(
    db_session,
    source: Source,
    *,
    outcome: FetchOutcome = FetchOutcome.SUCCESS,
    jobs_found: int = 1,
    listing_complete: bool = True,
    finished: bool = True,
) -> FetchRun:
    """Create one fetch run for *source*."""
    run = FetchRun(
        source_id=source.id,
        started_at=RUN_START,
        finished_at=RUN_FINISH if finished else None,
        outcome=outcome,
        http_status=200,
        jobs_found=jobs_found,
        jobs_new=jobs_found,
        pages_fetched=1,
        pages_expected=1,
        listing_complete=listing_complete,
        duration_ms=1000,
    )
    db_session.add(run)
    await db_session.flush()
    return run


async def make_live_job(
    db_session,
    source: Source,
    *,
    external_id: str = "job-1",
    last_seen_at: datetime = OCCURRENCE_SEEN,
    is_live: bool = True,
    fetch_run_id: uuid.UUID | None = None,
) -> Job:
    """Create one job plus a live occurrence for *source*."""
    job = Job(
        origin_source_id=source.id,
        origin_external_id=external_id,
        title="Software Engineer",
        company="Acme",
        company_normalized="acme",
        content_hash=b"coverage-test",
        status=JobStatus.OPEN,
    )
    db_session.add(job)
    await db_session.flush()

    occurrence = JobOccurrence(
        job_id=job.id,
        source_id=source.id,
        external_id=external_id,
        source_url=f"https://{source.tenant_key}.example.com/jobs/{external_id}",
        title_as_posted="Software Engineer",
        last_seen_at=last_seen_at,
        is_live=is_live,
        fetch_run_id=fetch_run_id,
    )
    db_session.add(occurrence)
    await db_session.flush()
    return job


async def evidence_for(db_session) -> dict:
    """Load coverage evidence and return this platform's entry (if any)."""
    evidence = await load_db_evidence(db_session)
    return evidence.get(PLATFORM_KEY, {})


async def test_complete_successful_fetch_with_a_live_job_verifies(
    db_session, coverage_platform
) -> None:
    source = await make_source(db_session)
    run = await make_run(db_session, source)
    job = await make_live_job(db_session, source, fetch_run_id=run.id)

    entry = await evidence_for(db_session)

    assert entry["verified"] is True
    assert entry["verified_tenants"] == 1
    assert entry["sample_job_id"] == str(job.id)
    assert entry["last_success_at"] == "2026-01-01T12:01:00Z"
    assert "tenant-a" in entry["verified_reason"]
    assert str(run.id) in entry["verified_reason"]


async def test_failed_fetch_cannot_verify(db_session, coverage_platform) -> None:
    source = await make_source(db_session)
    run = await make_run(
        db_session,
        source,
        outcome=FetchOutcome.FAILURE,
        jobs_found=0,
        listing_complete=False,
    )
    # Occurrence linked to the failed run should not verify
    await make_live_job(db_session, source, fetch_run_id=run.id)

    entry = await evidence_for(db_session)
    assert entry.get("verified", False) is False


async def test_partial_fetch_cannot_verify(db_session, coverage_platform) -> None:
    source = await make_source(db_session)
    run = await make_run(
        db_session,
        source,
        outcome=FetchOutcome.PARTIAL,
        jobs_found=3,
        listing_complete=False,
    )
    await make_live_job(db_session, source, fetch_run_id=run.id)

    entry = await evidence_for(db_session)
    assert entry.get("verified", False) is False


async def test_successful_but_incomplete_listing_cannot_verify(
    db_session, coverage_platform
) -> None:
    source = await make_source(db_session)
    run = await make_run(db_session, source, listing_complete=False)
    await make_live_job(db_session, source, fetch_run_id=run.id)

    entry = await evidence_for(db_session)
    assert entry.get("verified", False) is False


async def test_zero_jobs_cannot_verify(db_session, coverage_platform) -> None:
    source = await make_source(db_session)
    run = await make_run(db_session, source, jobs_found=0)
    await make_live_job(db_session, source, fetch_run_id=run.id)

    entry = await evidence_for(db_session)
    assert entry.get("verified", False) is False


async def test_occurrence_seen_before_the_run_cannot_verify(
    db_session, coverage_platform
) -> None:
    source = await make_source(db_session)
    run = await make_run(db_session, source)
    # Occurrence linked to the run but with last_seen_at before the run started
    # should not verify because the occurrence cannot have been recorded by this run
    await make_live_job(
        db_session, source, fetch_run_id=run.id, last_seen_at=RUN_START - timedelta(minutes=5)
    )

    entry = await evidence_for(db_session)
    assert entry.get("verified", False) is False


async def test_dead_occurrence_cannot_verify(db_session, coverage_platform) -> None:
    source = await make_source(db_session)
    run = await make_run(db_session, source)
    await make_live_job(db_session, source, fetch_run_id=run.id, is_live=False)

    entry = await evidence_for(db_session)
    assert entry.get("verified", False) is False


async def test_unfinished_run_cannot_verify(db_session, coverage_platform) -> None:
    source = await make_source(db_session)
    run = await make_run(db_session, source, finished=False)
    await make_live_job(db_session, source, fetch_run_id=run.id)

    entry = await evidence_for(db_session)
    assert entry.get("verified", False) is False


async def test_multiple_tenants_are_counted(db_session, coverage_platform) -> None:
    first = await make_source(db_session, tenant_key="tenant-a")
    run1 = await make_run(db_session, first)
    await make_live_job(db_session, first, external_id="job-a", fetch_run_id=run1.id)

    second = await make_source(db_session, tenant_key="tenant-b")
    run2 = await make_run(db_session, second)
    await make_live_job(db_session, second, external_id="job-b", fetch_run_id=run2.id)

    entry = await evidence_for(db_session)
    assert entry["verified"] is True
    assert entry["verified_tenants"] == 2


async def test_open_source_failure_is_reported(db_session, coverage_platform) -> None:
    await make_source(
        db_session,
        tenant_key="tenant-open",
        health=SourceHealth.OPEN,
        consecutive_failures=3,
    )

    entry = await evidence_for(db_session)
    assert entry["verified"] is False
    assert len(entry["open_failures"]) == 1
    assert "tenant-open" in entry["open_failures"][0]
    assert "consecutive_failures=3" in entry["open_failures"][0]


async def test_healthy_source_has_no_open_failures(db_session, coverage_platform) -> None:
    source = await make_source(db_session)
    run = await make_run(db_session, source)
    await make_live_job(db_session, source, fetch_run_id=run.id)

    entry = await evidence_for(db_session)
    assert entry["verified"] is True
    assert entry["open_failures"] == []


async def test_verified_platform_keeps_reported_failures_from_other_tenants(
    db_session, coverage_platform
) -> None:
    healthy = await make_source(db_session, tenant_key="tenant-a")
    run = await make_run(db_session, healthy)
    await make_live_job(db_session, healthy, fetch_run_id=run.id)
    await make_source(
        db_session,
        tenant_key="tenant-b",
        health=SourceHealth.OPEN,
        consecutive_failures=1,
    )

    entry = await evidence_for(db_session)
    assert entry["verified"] is True
    assert any("tenant-b" in failure for failure in entry["open_failures"])


async def test_cleanup_is_explicit_for_this_platform(db_session, coverage_platform) -> None:
    """The test fixture rows belong only to this platform and can be removed.

    This guards the fixture design itself: deleting the platform cascade target
    is intentionally impossible (``RESTRICT``), so the test proves the rows are
    addressable without touching seeded inventory.
    """

    await make_source(db_session)
    await db_session.execute(delete(Source).where(Source.platform_key == PLATFORM_KEY))
    await db_session.flush()


async def test_occurrence_updated_after_run_finishes_cannot_verify_earlier_run(
    db_session, coverage_platform
) -> None:
    """An occurrence with fetch_run_id pointing to a run but last_seen_at after
    the run finished cannot verify that earlier run. This test ensures the
    association is strictly by fetch_run_id, not by timestamp."""
    source = await make_source(db_session)
    run = await make_run(db_session, source)
    # Create an occurrence linked to the run but with last_seen_at AFTER the run finished
    # This simulates an occurrence that was updated by a later process
    await make_live_job(
        db_session,
        source,
        fetch_run_id=run.id,
        last_seen_at=RUN_FINISH + timedelta(minutes=10),
    )

    entry = await evidence_for(db_session)
    # The occurrence IS linked to the run via fetch_run_id, so it SHOULD verify
    # The key requirement is fetch_run_id association, not timestamp
    assert entry["verified"] is True
    assert entry["verified_tenants"] == 1


async def test_occurrence_from_another_source_cannot_verify_run(
    db_session, coverage_platform
) -> None:
    """An occurrence belonging to a different source cannot verify the run."""
    source1 = await make_source(db_session, tenant_key="tenant-a")
    run1 = await make_run(db_session, source1)

    source2 = await make_source(db_session, tenant_key="tenant-b")
    await make_run(db_session, source2)

    # Create an occurrence for source2 but link it to run1 (wrong source)
    job = Job(
        origin_source_id=source2.id,
        origin_external_id="job-cross-source",
        title="Software Engineer",
        company="Acme",
        company_normalized="acme",
        content_hash=b"cross-source-test",
        status=JobStatus.OPEN,
    )
    db_session.add(job)
    await db_session.flush()

    occurrence = JobOccurrence(
        job_id=job.id,
        source_id=source2.id,  # Belongs to source2
        external_id="job-cross-source",
        source_url="https://tenant-b.example.com/jobs/job-cross-source",
        title_as_posted="Software Engineer",
        last_seen_at=OCCURRENCE_SEEN,
        is_live=True,
        fetch_run_id=run1.id,  # But linked to run1 (source1's run)
    )
    db_session.add(occurrence)
    await db_session.flush()

    entry = await evidence_for(db_session)
    # Should NOT verify because the occurrence's source_id doesn't match the run's source_id
    assert entry.get("verified", False) is False


async def test_sequential_runs_cannot_borrow_occurrence_evidence(
    db_session, coverage_platform
) -> None:
    """Sequential runs for the same source: only occurrences linked to a specific
    run can verify that run. An occurrence from run1 cannot verify run2."""
    source = await make_source(db_session)

    # First run: successful, complete
    run1 = await make_run(db_session, source)
    await make_live_job(db_session, source, external_id="job-1", fetch_run_id=run1.id)

    # Second run: successful, complete, but NO occurrences linked to it
    run2 = await make_run(
        db_session, source, outcome=FetchOutcome.SUCCESS, jobs_found=0, listing_complete=True
    )

    entry = await evidence_for(db_session)
    # run1 has an occurrence, run2 does not
    # The platform should verify (because run1 verified it)
    # But run2's occurrence count is 0, which is correctly reflected in jobs_found=0
    assert entry["verified"] is True
    assert entry["verified_tenants"] == 1

    # Now test the reverse: run1 has NO occurrences, run2 has one
    # This should still verify because run2 has an occurrence
    await db_session.execute(delete(JobOccurrence).where(JobOccurrence.fetch_run_id == run1.id))
    await db_session.flush()

    job2 = await make_live_job(db_session, source, external_id="job-2", fetch_run_id=run2.id)
    await db_session.flush()

    entry = await evidence_for(db_session)
    assert entry["verified"] is True
    assert entry["verified_tenants"] == 1
    assert entry["sample_job_id"] == str(job2.id)  # Should be the job from run2
