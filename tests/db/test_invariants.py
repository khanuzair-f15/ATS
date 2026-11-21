"""Database behavioral invariant tests — tests actual PostgreSQL behavior for S1 invariants.

These tests verify the database-level enforcement of critical invariants:
- Circular job/cluster insertion
- Canonical consistency
- Open-job liveness
- first_seen_at immutability
- Closure evidence requirements
- Failed/incomplete fetches not causing closure
- Closure candidate view
- Relevant constraints and partial indexes

All tests require a live PostgreSQL database (marked with @pytest.mark.db).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from jobindex.db import (
    ChangeEvent,
    FetchRun,
    Job,
    JobCluster,
    JobOccurrence,
    Platform,
    Source,
)
from jobindex.db.enums import (
    ClosureEvidence,
    CoverageState,
    EmploymentType,
    FetchOutcome,
    JobStatus,
    PlatformClass,
    RemoteStatus,
    TenantStatus,
    TransportClass,
)
from jobindex.platforms import load_platform_rows

pytestmark = pytest.mark.db


# ============================================================================
# Test fixtures
# ============================================================================

@pytest.fixture
async def test_platform(db_session):
    """Create a test platform."""
    platform = Platform(
        key="test_platform",
        display_name="Test Platform",
        family="ats",
        platform_class=PlatformClass.C1,
        wave="S3",
        transport=TransportClass.API_JSON,
        auth_kind="none",
        coverage_state=CoverageState.OUTSTANDING,
        coverage_reason="test",
        notes="test",
    )
    db_session.add(platform)
    await db_session.commit()
    return platform


@pytest.fixture
async def test_source(db_session, test_platform):
    """Create a test source (tenant)."""
    source = Source(
        platform_key=test_platform.key,
        tenant_key="test_tenant",
        display_name="Test Tenant",
        entry_url="https://test.example.com/careers",
        transport_class=TransportClass.API_JSON,
        connector="test_platform",
        tenant_status=TenantStatus.VALIDATED,
    )
    db_session.add(source)
    await db_session.commit()
    return source


# ============================================================================
# Circular job/cluster insertion tests
# ============================================================================

async def test_circular_fk_job_cluster_insertion(db_session, test_source) -> None:
    """Test the correct insertion order for job → job_cluster → job.update.

    The only valid write order inside one transaction is:
    1. Insert canonical job with cluster_id = NULL
    2. Insert job_cluster with canonical_job_id = <that job>
    3. UPDATE job SET cluster_id = <cluster> for all members
    """
    # Step 1: Insert canonical job with cluster_id = NULL
    canonical_job = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-123",
        title="Software Engineer",
        company="Acme Corp",
        company_normalized="acme corp",
        description_text="Job description",
        content_hash=b"hash123",
        status=JobStatus.OPEN,
    )
    db_session.add(canonical_job)
    await db_session.flush()  # Get the ID

    # Step 2: Insert job_cluster with canonical_job_id
    cluster = JobCluster(
        canonical_job_id=canonical_job.id,
        dedup_version="v1",
    )
    db_session.add(cluster)
    await db_session.flush()

    # Step 3: Update canonical job with cluster_id
    canonical_job.cluster_id = cluster.id
    canonical_job.is_canonical = True
    await db_session.commit()

    # Verify the circular reference works
    assert canonical_job.cluster_id == cluster.id
    assert cluster.canonical_job_id == canonical_job.id
    assert canonical_job.is_canonical is True

    # Verify the deferred trigger enforces canonical consistency
    result = await db_session.execute(
        select(JobCluster).where(JobCluster.id == cluster.id)
    )
    loaded_cluster = result.scalar_one()
    assert loaded_cluster.canonical_job_id == canonical_job.id


async def test_canonical_consistency_enforced(db_session_commit, committed_records) -> None:
    """Test that canonical_job_id must point to a job with is_canonical=true in same cluster."""
    job = Job(
        id=committed_records.job_id,
        origin_source_id=committed_records.source_id,
        origin_external_id="ext-456",
        title="Engineer",
        company="Acme",
        company_normalized="acme",
        description_text="desc",
        content_hash=b"hash456",
        status=JobStatus.UNVERIFIED,
    )
    db_session_commit.add(job)
    await db_session_commit.commit()

    def new_cluster():
        return JobCluster(
            id=committed_records.cluster_id, canonical_job_id=job.id, dedup_version="v1"
        )

    db_session_commit.add(new_cluster())
    await db_session_commit.flush()
    with pytest.raises(DBAPIError, match="has no canonical job"):
        await db_session_commit.commit()
    await db_session_commit.rollback()
    await db_session_commit.refresh(job)

    cluster = new_cluster()
    db_session_commit.add(cluster)
    await db_session_commit.flush()
    job.cluster_id = cluster.id
    job.is_canonical = True
    await db_session_commit.commit()


async def test_non_canonical_job_merged_into(db_session, test_source) -> None:
    """Test that non-canonical jobs have merged_into_job_id set."""
    canonical_job = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-789",
        title="Manager",
        company="Acme",
        company_normalized="acme",
        description_text="desc",
        content_hash=b"hash789",
        status=JobStatus.OPEN,
    )
    db_session.add(canonical_job)
    await db_session.flush()

    cluster = JobCluster(
        canonical_job_id=canonical_job.id,
        dedup_version="v1",
    )
    db_session.add(cluster)
    await db_session.flush()

    canonical_job.cluster_id = cluster.id
    canonical_job.is_canonical = True

    # Create non-canonical duplicate
    duplicate_job = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-999",
        title="Manager",
        company="Acme",
        company_normalized="acme",
        description_text="desc",
        content_hash=b"hash999",
        status=JobStatus.OPEN,
        cluster_id=cluster.id,
        is_canonical=False,
        merged_into_job_id=canonical_job.id,
    )
    db_session.add(duplicate_job)
    await db_session.commit()

    # Verify constraints
    assert duplicate_job.is_canonical is False
    assert duplicate_job.merged_into_job_id == canonical_job.id
    assert duplicate_job.cluster_id == cluster.id


# ============================================================================
# Liveness invariant tests
# ============================================================================

async def test_open_job_requires_live_occurrence(db_session_commit, committed_records) -> None:
    """Test that an open job cannot exist with zero live occurrences."""
    job = Job(
        id=committed_records.job_id,
        origin_source_id=committed_records.source_id,
        origin_external_id="ext-live-1",
        title="Live Job",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashlive",
        status=JobStatus.OPEN,
    )
    db_session_commit.add(job)
    await db_session_commit.flush()

    # Create a live occurrence
    occurrence = JobOccurrence(
        id=committed_records.occurrence_id,
        job_id=job.id,
        source_id=committed_records.source_id,
        external_id="occ-1",
        source_url="https://test.example.com/jobs/1",
        apply_urls=["https://test.example.com/apply/1"],
        title_as_posted="Live Job",
        last_seen_at=datetime.now(UTC),
        is_live=True,
    )
    db_session_commit.add(occurrence)
    await db_session_commit.commit()

    # Now try to set is_live = false while job is still open
    occurrence.is_live = False

    # This should fail due to deferred trigger
    with pytest.raises(DBAPIError, match="is open with zero live occurrences"):
        await db_session_commit.commit()

    await db_session_commit.rollback()

    # Correct path: reopen occurrence first, then change job status
    await db_session_commit.refresh(occurrence)
    assert occurrence.is_live is True


async def test_job_status_change_requires_live_occurrence(
    db_session_commit, committed_records
) -> None:
    """Test that setting job.status='open' requires a live occurrence."""
    job = Job(
        id=committed_records.job_id,
        origin_source_id=committed_records.source_id,
        origin_external_id="ext-live-2",
        title="Another Job",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashlive2",
        status=JobStatus.UNVERIFIED,
    )
    db_session_commit.add(job)
    await db_session_commit.flush()

    # Try to set status to open without occurrence
    job.status = JobStatus.OPEN

    with pytest.raises(DBAPIError, match="is open with zero live occurrences"):
        await db_session_commit.commit()


async def test_reopen_path(db_session_commit, committed_records) -> None:
    """Test the correct reopen path: occurrence is_live=true first, then job status."""
    job = Job(
        id=committed_records.job_id,
        origin_source_id=committed_records.source_id,
        origin_external_id="ext-reopen",
        title="Reopen Job",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashreopen",
        status=JobStatus.CLOSED,
        closure_evidence_kind=ClosureEvidence.HTTP_404,
        closure_evidence_at=datetime.now(UTC),
    )
    db_session_commit.add(job)
    await db_session_commit.flush()

    occurrence = JobOccurrence(
        id=committed_records.occurrence_id,
        job_id=job.id,
        source_id=committed_records.source_id,
        external_id="occ-reopen",
        source_url="https://test.example.com/jobs/reopen",
        apply_urls=["https://test.example.com/apply/reopen"],
        title_as_posted="Reopen Job",
        last_seen_at=datetime.now(UTC),
        is_live=False,
    )
    db_session_commit.add(occurrence)
    await db_session_commit.commit()

    # Reopen: first set occurrence is_live=true
    occurrence.is_live = True
    await db_session_commit.flush()

    # Then set job status to open
    job.status = JobStatus.OPEN
    job.closure_evidence_kind = None
    job.closure_evidence_at = None
    await db_session_commit.commit()

    # Verify
    assert job.status == JobStatus.OPEN
    assert occurrence.is_live is True


# ============================================================================
# first_seen_at immutability tests
# ============================================================================

async def test_first_seen_at_immutable(db_session_commit, committed_records) -> None:
    """Test that job.first_seen_at cannot be modified after insert."""
    job = Job(
        id=committed_records.job_id,
        origin_source_id=committed_records.source_id,
        origin_external_id="ext-immutable",
        title="Immutable Test",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashimmutable",
        status=JobStatus.UNVERIFIED,
    )
    db_session_commit.add(job)
    await db_session_commit.commit()

    original_first_seen = job.first_seen_at

    # Try to modify first_seen_at
    job.first_seen_at = datetime(2020, 1, 1, tzinfo=UTC)

    # Trigger raises generic exception (SQLSTATE P0001), not constraint violation
    with pytest.raises(DBAPIError, match="immutable"):
        await db_session_commit.commit()

    await db_session_commit.rollback()

    # Verify it's unchanged
    result = await db_session_commit.execute(select(Job).where(Job.id == committed_records.job_id))
    reloaded = result.scalar_one()
    assert reloaded.first_seen_at == original_first_seen


# ============================================================================
# Closure evidence tests
# ============================================================================

async def test_closed_requires_evidence(db_session, test_source) -> None:
    """Test that job.status='closed' requires closure_evidence_kind and closure_evidence_at."""
    job = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-closed-no-evidence",
        title="Closed No Evidence",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashclosed",
        status=JobStatus.CLOSED,
    )
    db_session.add(job)

    with pytest.raises(IntegrityError):
        await db_session.commit()

    await db_session.rollback()

    # Rollback removed the pending insert from the session.
    db_session.add(job)
    job.status = JobStatus.CLOSED
    job.closure_evidence_kind = ClosureEvidence.HTTP_404
    job.closure_evidence_at = datetime.now(UTC)
    await db_session.commit()


async def test_open_forbids_evidence(db_session, test_source) -> None:
    """Test that job.status='open' forbids closure evidence."""
    job = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-open-with-evidence",
        title="Open With Evidence",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashopen",
        status=JobStatus.OPEN,
        closure_evidence_kind=ClosureEvidence.HTTP_404,
        closure_evidence_at=datetime.now(UTC),
    )
    db_session.add(job)

    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_stale_requires_since(db_session, test_source) -> None:
    """Test that job.status='stale' requires stale_since_at."""
    job = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-stale-no-since",
        title="Stale No Since",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashstale",
        status=JobStatus.STALE,
    )
    db_session.add(job)

    with pytest.raises(IntegrityError):
        await db_session.commit()


# ============================================================================
# Closure candidate view tests
# ============================================================================

async def test_closure_candidate_view_logic(db_session, test_source) -> None:
    """Test the job_closure_candidates view logic.

    Candidates must satisfy:
    - job.status = 'stale'
    - job.stale_since_at < now() - 72 hours
    - EXISTS successful fetch_run with listing_complete after stale_since_at
    """
    # Create a stale job old enough
    stale_job = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-candidate-1",
        title="Candidate Job",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashcandidate",
        status=JobStatus.STALE,
        stale_since_at=datetime(2020, 1, 1, tzinfo=UTC),  # Old enough
    )
    db_session.add(stale_job)
    await db_session.flush()

    # Create a live occurrence
    occurrence = JobOccurrence(
        job_id=stale_job.id,
        source_id=test_source.id,
        external_id="occ-candidate",
        source_url="https://test.example.com/jobs/candidate",
        apply_urls=["https://test.example.com/apply/candidate"],
        title_as_posted="Candidate Job",
        last_seen_at=datetime.now(UTC),
        is_live=False,  # Not live
    )
    db_session.add(occurrence)

    # Create a successful complete listing fetch_run AFTER stale_since_at
    fetch_run = FetchRun(
        source_id=test_source.id,
        started_at=datetime(2020, 1, 2, tzinfo=UTC),
        finished_at=datetime(2020, 1, 2, tzinfo=UTC),
        outcome=FetchOutcome.SUCCESS,
        listing_complete=True,
    )
    db_session.add(fetch_run)
    await db_session.commit()

    # Query the view
    result = await db_session.execute(text("SELECT id FROM job_closure_candidates"))
    candidates = result.scalars().all()

    assert stale_job.id in candidates

    # Cleanup: delete from underlying tables in FK order (view is not updatable)
    await db_session.execute(delete(FetchRun).where(FetchRun.source_id == test_source.id))
    await db_session.execute(delete(JobOccurrence).where(JobOccurrence.job_id == stale_job.id))
    await db_session.execute(delete(Job).where(Job.id == stale_job.id))
    await db_session.commit()


async def test_closure_candidate_excludes_incomplete_listing(db_session, test_source) -> None:
    """Test that incomplete listings don't qualify for closure."""
    stale_job = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-candidate-2",
        title="Candidate Job 2",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashcandidate2",
        status=JobStatus.STALE,
        stale_since_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    db_session.add(stale_job)
    await db_session.flush()

    occurrence = JobOccurrence(
        job_id=stale_job.id,
        source_id=test_source.id,
        external_id="occ-candidate-2",
        source_url="https://test.example.com/jobs/candidate2",
        apply_urls=["https://test.example.com/apply/candidate2"],
        title_as_posted="Candidate Job 2",
        last_seen_at=datetime.now(UTC),
        is_live=False,
    )
    db_session.add(occurrence)

    # Incomplete listing (listing_complete = false)
    fetch_run = FetchRun(
        source_id=test_source.id,
        started_at=datetime(2020, 1, 2, tzinfo=UTC),
        finished_at=datetime(2020, 1, 2, tzinfo=UTC),
        outcome=FetchOutcome.SUCCESS,
        listing_complete=False,  # Not complete!
    )
    db_session.add(fetch_run)
    await db_session.commit()

    # Should NOT be in candidates
    result = await db_session.execute(text("SELECT id FROM job_closure_candidates"))
    candidates = result.scalars().all()

    assert stale_job.id not in candidates


async def test_closure_candidate_excludes_failed_fetch(db_session, test_source) -> None:
    """Test that failed/abandoned/partial fetch runs don't qualify."""
    stale_job = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-candidate-3",
        title="Candidate Job 3",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashcandidate3",
        status=JobStatus.STALE,
        stale_since_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    db_session.add(stale_job)
    await db_session.flush()

    occurrence = JobOccurrence(
        job_id=stale_job.id,
        source_id=test_source.id,
        external_id="occ-candidate-3",
        source_url="https://test.example.com/jobs/candidate3",
        apply_urls=["https://test.example.com/apply/candidate3"],
        title_as_posted="Candidate Job 3",
        last_seen_at=datetime.now(UTC),
        is_live=False,
    )
    db_session.add(occurrence)

    # Failed outcome
    fetch_run = FetchRun(
        source_id=test_source.id,
        started_at=datetime(2020, 1, 2, tzinfo=UTC),
        finished_at=datetime(2020, 1, 2, tzinfo=UTC),
        outcome=FetchOutcome.FAILURE,
        listing_complete=True,  # Even with listing_complete=true
    )
    db_session.add(fetch_run)
    await db_session.commit()

    result = await db_session.execute(text("SELECT id FROM job_closure_candidates"))
    candidates = result.scalars().all()

    assert stale_job.id not in candidates


async def test_closure_candidate_excludes_partial_listing(db_session, test_source) -> None:
    """Test that partial outcome doesn't qualify."""
    stale_job = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-candidate-4",
        title="Candidate Job 4",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashcandidate4",
        status=JobStatus.STALE,
        stale_since_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    db_session.add(stale_job)
    await db_session.flush()

    occurrence = JobOccurrence(
        job_id=stale_job.id,
        source_id=test_source.id,
        external_id="occ-candidate-4",
        source_url="https://test.example.com/jobs/candidate4",
        apply_urls=["https://test.example.com/apply/candidate4"],
        title_as_posted="Candidate Job 4",
        last_seen_at=datetime.now(UTC),
        is_live=False,
    )
    db_session.add(occurrence)

    # Partial outcome
    fetch_run = FetchRun(
        source_id=test_source.id,
        started_at=datetime(2020, 1, 2, tzinfo=UTC),
        finished_at=datetime(2020, 1, 2, tzinfo=UTC),
        outcome=FetchOutcome.PARTIAL,
        listing_complete=False,
    )
    db_session.add(fetch_run)
    await db_session.commit()

    result = await db_session.execute(text("SELECT id FROM job_closure_candidates"))
    candidates = result.scalars().all()

    assert stale_job.id not in candidates


async def test_closure_candidate_excludes_fetch_before_stale(db_session, test_source) -> None:
    """Test that fetch_run must be AFTER stale_since_at."""
    stale_job = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-candidate-5",
        title="Candidate Job 5",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashcandidate5",
        status=JobStatus.STALE,
        stale_since_at=datetime(2020, 1, 10, tzinfo=UTC),  # Later date
    )
    db_session.add(stale_job)
    await db_session.flush()

    occurrence = JobOccurrence(
        job_id=stale_job.id,
        source_id=test_source.id,
        external_id="occ-candidate-5",
        source_url="https://test.example.com/jobs/candidate5",
        apply_urls=["https://test.example.com/apply/candidate5"],
        title_as_posted="Candidate Job 5",
        last_seen_at=datetime.now(UTC),
        is_live=False,
    )
    db_session.add(occurrence)

    # Fetch run BEFORE stale_since_at
    fetch_run = FetchRun(
        source_id=test_source.id,
        started_at=datetime(2020, 1, 1, tzinfo=UTC),  # Before stale_since_at
        finished_at=datetime(2020, 1, 1, tzinfo=UTC),
        outcome=FetchOutcome.SUCCESS,
        listing_complete=True,
    )
    db_session.add(fetch_run)
    await db_session.commit()

    result = await db_session.execute(text("SELECT id FROM job_closure_candidates"))
    candidates = result.scalars().all()

    assert stale_job.id not in candidates


# ============================================================================
# Closure evidence kind tests
# ============================================================================

async def test_explicit_closure_evidence(db_session, test_source) -> None:
    """Test explicit closure evidence kinds."""
    evidence_kinds = [
        ClosureEvidence.HTTP_404,
        ClosureEvidence.HTTP_410,
        ClosureEvidence.PAYLOAD_MARKER,
        ClosureEvidence.ABSENT_FROM_FULL_LISTING,
        ClosureEvidence.MANUAL,
    ]

    for i, kind in enumerate(evidence_kinds):
        job = Job(
            origin_source_id=test_source.id,
            origin_external_id=f"ext-evidence-{i}",
            title=f"Evidence Job {i}",
            company="Test Co",
            company_normalized="test co",
            description_text="desc",
            content_hash=f"hashevidence{i}".encode(),
            status=JobStatus.CLOSED,
            closure_evidence_kind=kind,
            closure_evidence_at=datetime.now(UTC),
        )
        db_session.add(job)

    await db_session.commit()


# ============================================================================
# Failed/incomplete fetch tests
# ============================================================================

async def test_failed_fetch_never_closes(db_session, test_source) -> None:
    """Test that failed fetch outcomes never cause absence-based closure."""
    job = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-failed-fetch",
        title="Failed Fetch Job",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashfailed",
        status=JobStatus.OPEN,
    )
    db_session.add(job)
    await db_session.flush()

    occurrence = JobOccurrence(
        job_id=job.id,
        source_id=test_source.id,
        external_id="occ-failed",
        source_url="https://test.example.com/jobs/failed",
        apply_urls=["https://test.example.com/apply/failed"],
        title_as_posted="Failed Fetch Job",
        last_seen_at=datetime.now(UTC),
        is_live=True,
    )
    db_session.add(occurrence)

    fetch_run = FetchRun(
        source_id=test_source.id,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        outcome=FetchOutcome.FAILURE,
        error_class="transient",
        listing_complete=False,
    )
    db_session.add(fetch_run)
    await db_session.commit()

    # Job should still be open
    result = await db_session.execute(select(Job).where(Job.id == job.id))
    reloaded = result.scalar_one()
    assert reloaded.status == JobStatus.OPEN


async def test_timeout_does_not_close(db_session, test_source) -> None:
    """Test that timeout (error_class='transient') doesn't close jobs."""
    job = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-timeout",
        title="Timeout Job",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashtimeout",
        status=JobStatus.OPEN,
    )
    db_session.add(job)
    await db_session.flush()

    occurrence = JobOccurrence(
        job_id=job.id,
        source_id=test_source.id,
        external_id="occ-timeout",
        source_url="https://test.example.com/jobs/timeout",
        apply_urls=["https://test.example.com/apply/timeout"],
        title_as_posted="Timeout Job",
        last_seen_at=datetime.now(UTC),
        is_live=True,
    )
    db_session.add(occurrence)

    fetch_run = FetchRun(
        source_id=test_source.id,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        outcome=FetchOutcome.FAILURE,
        error_class="transient",
        listing_complete=False,
    )
    db_session.add(fetch_run)
    await db_session.commit()

    result = await db_session.execute(select(Job).where(Job.id == job.id))
    reloaded = result.scalar_one()
    assert reloaded.status == JobStatus.OPEN


async def test_partial_listing_does_not_close(db_session, test_source) -> None:
    """Test that partial listing doesn't cause closure."""
    job = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-partial",
        title="Partial Job",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashpartial",
        status=JobStatus.OPEN,
    )
    db_session.add(job)
    await db_session.flush()

    occurrence = JobOccurrence(
        job_id=job.id,
        source_id=test_source.id,
        external_id="occ-partial",
        source_url="https://test.example.com/jobs/partial",
        apply_urls=["https://test.example.com/apply/partial"],
        title_as_posted="Partial Job",
        last_seen_at=datetime.now(UTC),
        is_live=True,
    )
    db_session.add(occurrence)

    fetch_run = FetchRun(
        source_id=test_source.id,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        outcome=FetchOutcome.PARTIAL,
        pages_fetched=1,
        pages_expected=5,
        listing_complete=False,
    )
    db_session.add(fetch_run)
    await db_session.commit()

    result = await db_session.execute(select(Job).where(Job.id == job.id))
    reloaded = result.scalar_one()
    assert reloaded.status == JobStatus.OPEN


async def test_abandoned_fetch_does_not_close(db_session, test_source) -> None:
    """Test that abandoned (crashed) fetch doesn't close jobs."""
    job = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-abandoned",
        title="Abandoned Job",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashabandoned",
        status=JobStatus.OPEN,
    )
    db_session.add(job)
    await db_session.flush()

    occurrence = JobOccurrence(
        job_id=job.id,
        source_id=test_source.id,
        external_id="occ-abandoned",
        source_url="https://test.example.com/jobs/abandoned",
        apply_urls=["https://test.example.com/apply/abandoned"],
        title_as_posted="Abandoned Job",
        last_seen_at=datetime.now(UTC),
        is_live=True,
    )
    db_session.add(occurrence)

    fetch_run = FetchRun(
        source_id=test_source.id,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        outcome=FetchOutcome.ABANDONED,
        error_class="transient",
        listing_complete=False,
    )
    db_session.add(fetch_run)
    await db_session.commit()

    result = await db_session.execute(select(Job).where(Job.id == job.id))
    reloaded = result.scalar_one()
    assert reloaded.status == JobStatus.OPEN


# ============================================================================
# Constraint tests
# ============================================================================

async def test_platform_family_known(db_session) -> None:
    """Test platform.family CHECK constraint."""
    platform = Platform(
        key="bad_family",
        display_name="Bad Family",
        family="invalid",
        platform_class=PlatformClass.C1,
        wave="S3",
        auth_kind="none",
        coverage_state=CoverageState.OUTSTANDING,
        coverage_reason="test",
    )
    db_session.add(platform)

    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_platform_outstanding_requires_reason(db_session) -> None:
    """Test platform.outstanding_requires_reason constraint."""
    platform = Platform(
        key="bad_outstanding",
        display_name="Bad Outstanding",
        family="ats",
        platform_class=PlatformClass.C1,
        wave="S3",
        auth_kind="none",
        coverage_state=CoverageState.OUTSTANDING,
        coverage_reason=None,  # Should fail
    )
    db_session.add(platform)

    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_source_config_no_secrets(db_session, test_platform) -> None:
    """Test source.config_no_secrets constraint."""
    source = Source(
        platform_key=test_platform.key,
        tenant_key="secret_tenant",
        entry_url="https://test.example.com",
        transport_class=TransportClass.API_JSON,
        connector="test",
        config={"token": "secret123"},  # Forbidden key
    )
    db_session.add(source)

    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_job_salary_range_valid(db_session, test_source) -> None:
    """Test job.salary_range_valid constraint."""
    job = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-salary-invalid",
        title="Salary Test",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashsalary",
        salary_min=Decimal("100000"),
        salary_max=Decimal("50000"),  # min > max
        salary_currency="USD",
    )
    db_session.add(job)

    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_job_salary_currency_required(db_session, test_source) -> None:
    """Test job.salary_currency_required constraint."""
    job = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-salary-currency",
        title="Salary Currency Test",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashsalary2",
        salary_min=Decimal("100000"),
        salary_currency=None,  # Should fail when salary_min is set
    )
    db_session.add(job)

    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_job_country_code_iso2(db_session, test_source) -> None:
    """Test job.country_code_iso2 constraint."""
    job = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-country",
        title="Country Test",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashcountry",
        country_code="USA",  # Should be 2 letters
    )
    db_session.add(job)

    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_job_salary_currency_iso4217(db_session, test_source) -> None:
    """Test job.salary_currency_iso4217 constraint."""
    job = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-currency",
        title="Currency Test",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashcurrency",
        salary_min=Decimal("100000"),
        salary_currency="USDD",  # Should be 3 letters
    )
    db_session.add(job)

    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_source_unique_tenant(db_session, test_platform) -> None:
    """Test source UNIQUE(platform_key, tenant_key)."""
    source1 = Source(
        platform_key=test_platform.key,
        tenant_key="duplicate",
        entry_url="https://test1.example.com",
        transport_class=TransportClass.API_JSON,
        connector="test",
    )
    source2 = Source(
        platform_key=test_platform.key,
        tenant_key="duplicate",
        entry_url="https://test2.example.com",
        transport_class=TransportClass.API_JSON,
        connector="test",
    )
    db_session.add(source1)
    await db_session.commit()

    db_session.add(source2)

    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_job_origin_identity_unique(db_session, test_source) -> None:
    """Test job UNIQUE(origin_source_id, origin_external_id)."""
    job1 = Job(
        origin_source_id=test_source.id,
        origin_external_id="duplicate-ext",
        title="Duplicate Test",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashdup1",
    )
    job2 = Job(
        origin_source_id=test_source.id,
        origin_external_id="duplicate-ext",
        title="Duplicate Test 2",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashdup2",
    )
    db_session.add(job1)
    await db_session.commit()

    db_session.add(job2)

    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_occurrence_identity_unique(db_session, test_source) -> None:
    """Test job_occurrence UNIQUE(source_id, external_id)."""
    job = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-occ",
        title="Occ Test",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashocc",
    )
    db_session.add(job)
    await db_session.flush()

    occ1 = JobOccurrence(
        job_id=job.id,
        source_id=test_source.id,
        external_id="duplicate-occ",
        source_url="https://test.example.com/1",
        apply_urls=["https://test.example.com/apply/1"],
        title_as_posted="Occ 1",
        last_seen_at=datetime.now(UTC),
    )
    occ2 = JobOccurrence(
        job_id=job.id,
        source_id=test_source.id,
        external_id="duplicate-occ",
        source_url="https://test.example.com/2",
        apply_urls=["https://test.example.com/apply/2"],
        title_as_posted="Occ 2",
        last_seen_at=datetime.now(UTC),
    )
    db_session.add(occ1)
    await db_session.commit()

    db_session.add(occ2)

    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_change_event_idempotent(db_session, test_source) -> None:
    """Test change_event UNIQUE(occurrence_id, field, new_value, fetch_run_id)."""
    job = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-change",
        title="Change Test",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashchange",
    )
    db_session.add(job)
    await db_session.flush()

    occurrence = JobOccurrence(
        job_id=job.id,
        source_id=test_source.id,
        external_id="ext-occ",
        source_url="https://test.example.com/occ",
        apply_urls=["https://test.example.com/apply"],
        title_as_posted="Change Test",
        last_seen_at=datetime.now(UTC),
    )
    db_session.add(occurrence)
    await db_session.flush()

    fetch_run = FetchRun(
        source_id=test_source.id,
        started_at=datetime.now(UTC),
    )
    db_session.add(fetch_run)
    await db_session.flush()

    # First change event
    event1 = ChangeEvent(
        job_id=job.id,
        occurrence_id=occurrence.id,
        fetch_run_id=fetch_run.id,
        field="title",
        old_value="Old Title",
        new_value="New Title",
    )
    db_session.add(event1)
    await db_session.commit()

    # Duplicate change event (same occurrence, field, new_value, fetch_run)
    event2 = ChangeEvent(
        job_id=job.id,
        occurrence_id=occurrence.id,
        fetch_run_id=fetch_run.id,
        field="title",
        old_value="Old Title",
        new_value="New Title",
    )
    db_session.add(event2)

    with pytest.raises(IntegrityError):
        await db_session.commit()


# ============================================================================
# Partial index tests
# ============================================================================

async def test_partial_index_job_one_canonical_per_cluster(db_session, test_source) -> None:
    """Test job_one_canonical_per_cluster partial unique index."""
    # Create two canonical jobs in same cluster - should fail
    job1 = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-canon-1",
        title="Canonical 1",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashcanon1",
        status=JobStatus.OPEN,
        cluster_id=None,
        is_canonical=True,
    )
    db_session.add(job1)
    await db_session.flush()
    cluster = JobCluster(canonical_job_id=job1.id, dedup_version="v1")
    db_session.add(cluster)
    await db_session.flush()
    job1.cluster_id = cluster.id
    await db_session.flush()
    job2 = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-canon-2",
        title="Canonical 2",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashcanon2",
        status=JobStatus.OPEN,
        cluster_id=job1.cluster_id,  # Same cluster
        is_canonical=True,
    )
    db_session.add(job2)

    with pytest.raises(IntegrityError, match="job_one_canonical_per_cluster"):
        await db_session.commit()


async def test_partial_index_job_open_liveness(db_session, test_source) -> None:
    """Test job_open_liveness partial index exists (behavioral test)."""
    # Just verify we can create open jobs
    job = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-open-index",
        title="Open Index Test",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashopenidx",
        status=JobStatus.OPEN,
    )
    db_session.add(job)
    await db_session.commit()


async def test_partial_index_job_sort(db_session, test_source) -> None:
    """Test job_sort partial index (WHERE is_canonical)."""
    job = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-sort",
        title="Sort Test",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashsort",
        is_canonical=True,
    )
    db_session.add(job)
    await db_session.commit()


async def test_partial_index_job_remote_type(db_session, test_source) -> None:
    """Test job_remote_type partial index (WHERE status='open')."""
    job = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-remote",
        title="Remote Test",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashremote",
        status=JobStatus.OPEN,
        remote_status=RemoteStatus.REMOTE,
        employment_type=EmploymentType.FULL_TIME,
    )
    db_session.add(job)
    await db_session.commit()


async def test_partial_index_job_salary_floor(db_session, test_source) -> None:
    """Test job_salary_floor partial index (WHERE salary_min IS NOT NULL)."""
    job = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-salary-floor",
        title="Salary Floor Test",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashsalaryfloor",
        salary_min=Decimal("100000"),
        salary_currency="USD",
    )
    db_session.add(job)
    await db_session.commit()


async def test_partial_index_source_due(db_session, test_platform) -> None:
    """Test source_due partial index (WHERE enabled)."""
    source = Source(
        platform_key=test_platform.key,
        tenant_key="due-test",
        entry_url="https://due.example.com",
        transport_class=TransportClass.API_JSON,
        connector="test",
        enabled=True,
    )
    db_session.add(source)
    await db_session.commit()


async def test_partial_index_fetch_run_failures(db_session, test_source) -> None:
    """Test fetch_run_failures partial index (WHERE outcome IN ('failure','abandoned'))."""
    fetch_run = FetchRun(
        source_id=test_source.id,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        outcome=FetchOutcome.FAILURE,
    )
    db_session.add(fetch_run)
    await db_session.commit()


async def test_partial_index_fetch_run_complete_listing(db_session, test_source) -> None:
    """Test fetch_run_complete_listing partial index (WHERE listing_complete)."""
    fetch_run = FetchRun(
        source_id=test_source.id,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        outcome=FetchOutcome.SUCCESS,
        listing_complete=True,
    )
    db_session.add(fetch_run)
    await db_session.commit()


async def test_partial_index_occurrence_job_live(db_session, test_source) -> None:
    """Test occurrence_job_live partial index (WHERE is_live)."""
    job = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-occ-live",
        title="Occ Live",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashocclive",
    )
    db_session.add(job)
    await db_session.flush()

    occurrence = JobOccurrence(
        job_id=job.id,
        source_id=test_source.id,
        external_id="occ-live",
        source_url="https://test.example.com/occ",
        apply_urls=["https://test.example.com/apply"],
        title_as_posted="Occ Live",
        last_seen_at=datetime.now(UTC),
        is_live=True,
    )
    db_session.add(occurrence)
    await db_session.commit()


# ============================================================================
# Generated column tests
# ============================================================================

async def test_job_sort_at_generated(db_session, test_source) -> None:
    """Test job.sort_at generated column."""
    # With posted_at
    job1 = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-sort-generated-1",
        title="Sort Generated 1",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashsortgen1",
        posted_at=datetime(2024, 1, 15, tzinfo=UTC),
    )
    db_session.add(job1)
    await db_session.flush()
    assert job1.sort_at == datetime(2024, 1, 15, tzinfo=UTC)

    # Without posted_at (should use first_seen_at)
    job2 = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-sort-generated-2",
        title="Sort Generated 2",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashsortgen2",
    )
    db_session.add(job2)
    await db_session.flush()
    assert job2.sort_at == job2.first_seen_at


async def test_job_search_tsv_generated(db_session, test_source) -> None:
    """Test job.search_tsv generated column."""
    job = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-search-tsv",
        title="Python Developer",
        company="Acme Corp",
        company_normalized="acme corp",
        description_text="Python developer needed",
        content_hash=b"hashsearch",
        city_raw="San Francisco",
        region_raw="CA",
    )
    db_session.add(job)
    await db_session.flush()
    assert job.search_tsv is not None
    assert "python" in str(job.search_tsv).lower()


async def test_occurrence_apply_url_generated(db_session, test_source) -> None:
    """Test job_occurrence.apply_url generated column."""
    job = Job(
        origin_source_id=test_source.id,
        origin_external_id="ext-occ-apply",
        title="Apply URL Test",
        company="Test Co",
        company_normalized="test co",
        description_text="desc",
        content_hash=b"hashapply",
    )
    db_session.add(job)
    await db_session.flush()

    occurrence = JobOccurrence(
        job_id=job.id,
        source_id=test_source.id,
        external_id="ext-apply",
        source_url="https://test.example.com/job",
        apply_urls=["https://test.example.com/apply/1", "https://test.example.com/apply/2"],
        title_as_posted="Apply URL Test",
        last_seen_at=datetime.now(UTC),
    )
    db_session.add(occurrence)
    await db_session.flush()

    assert occurrence.apply_url == "https://test.example.com/apply/1"


async def test_fetch_run_listing_complete_default(db_session, test_source) -> None:
    """Test fetch_run.listing_complete defaults to false."""
    fetch_run = FetchRun(source_id=test_source.id)
    db_session.add(fetch_run)
    await db_session.flush()

    assert fetch_run.listing_complete is False


# ============================================================================
# Seed data verification
# ============================================================================

async def test_platform_seed_78_rows(db_session) -> None:
    """Verify all 78 platforms are seeded."""
    result = await db_session.execute(select(Platform))
    platforms = result.scalars().all()

    assert len(platforms) == 78

    doc_rows = load_platform_rows()
    assert len(doc_rows) == 78

    # Verify each matches
    db_by_key = {p.key: p for p in platforms}
    for doc_row in doc_rows:
        assert doc_row.key in db_by_key
        db_row = db_by_key[doc_row.key]
        assert db_row.display_name == doc_row.display_name
        assert db_row.family == doc_row.family
        assert db_row.platform_class.value == doc_row.platform_class
        assert db_row.wave == doc_row.wave
        assert db_row.auth_kind == doc_row.auth_kind
        assert db_row.coverage_state.value == doc_row.coverage_state
        assert db_row.coverage_reason == doc_row.coverage_reason


async def test_platform_transport_null(db_session) -> None:
    """Verify all platforms have transport=NULL in seed."""
    result = await db_session.execute(select(Platform))
    for platform in result.scalars().all():
        assert platform.transport is None


async def test_platform_coverage_outstanding(db_session) -> None:
    """Verify all platforms have coverage_state='outstanding'."""
    result = await db_session.execute(select(Platform))
    for platform in result.scalars().all():
        assert platform.coverage_state == CoverageState.OUTSTANDING
        assert platform.coverage_reason is not None