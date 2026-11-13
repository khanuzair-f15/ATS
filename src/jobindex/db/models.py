"""SQLAlchemy models for the S1 baseline table set (`docs/schema.md` §13).

The Alembic baseline is hand-written DDL that mirrors `docs/schema.md` exactly; these
models are the application's typed view of the same tables. `tests/test_schema_parity.py`
asserts the two agree on table and column names, so they cannot drift silently.

Deliberately absent in S1: `source_run_queue` (S5), `dedup_candidate`,
`dedup_rejected_edge`, `job_description_shingle` (S9).
"""

from __future__ import annotations

import sqlalchemy
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from jobindex.db.base import Base, created_at, updated_at, uuid_pk
from jobindex.db.enums import (
    ClosureEvidence,
    CoverageState,
    EmploymentType,
    ErrorClass,
    FetchOutcome,
    JobStatus,
    PlatformClass,
    RefreshTier,
    RemoteStatus,
    SalaryPeriod,
    SourceHealth,
    TenantStatus,
    TransportClass,
    VerificationStatus,
)

# The generated `search_tsv` expression. ADR 0004 defers the definition to
# `docs/schema.md` §5, which now states it verbatim; this constant is the same string.
SEARCH_TSV_SQL = (
    "to_tsvector('english', "
    "coalesce(title, '') || ' ' || coalesce(company, '') || ' ' || "
    "coalesce(city_raw, '') || ' ' || coalesce(region_raw, '') || ' ' || "
    "coalesce(description_text, ''))"
)


def _enum(enum_cls: type, name: str) -> sqlalchemy.Enum:
    from sqlalchemy import Enum as SAEnum

    return SAEnum(
        enum_cls,
        name=name,
        native_enum=True,
        create_constraint=False,
        values_callable=lambda cls: [member.value for member in cls],
    )


class Platform(Base):
    __tablename__ = "platform"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    family: Mapped[str] = mapped_column(Text, nullable=False)
    platform_class: Mapped[PlatformClass] = mapped_column(
        "class", _enum(PlatformClass, "platform_class"), nullable=False
    )
    wave: Mapped[str] = mapped_column(Text, nullable=False)
    transport: Mapped[TransportClass | None] = mapped_column(
        _enum(TransportClass, "transport_class"), nullable=True
    )
    auth_kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'none'"))
    coverage_state: Mapped[CoverageState] = mapped_column(
        _enum(CoverageState, "coverage_state"),
        nullable=False,
        server_default=text("'outstanding'"),
    )
    coverage_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    sources: Mapped[list[Source]] = relationship(back_populates="platform")

    __table_args__ = (
        CheckConstraint(
            "coverage_state <> 'outstanding' OR coverage_reason IS NOT NULL",
            name="outstanding_requires_reason",
        ),
        CheckConstraint("family IN ('ats', 'board')", name="family_known"),
    )


class Source(Base):
    """One row per **tenant**, not per platform (`docs/schema.md` §4)."""

    __tablename__ = "source"

    id: Mapped[uuid.UUID] = uuid_pk()
    platform_key: Mapped[str] = mapped_column(
        Text, ForeignKey("platform.key", ondelete="RESTRICT"), nullable=False
    )
    tenant_key: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    entry_url: Mapped[str] = mapped_column(Text, nullable=False)
    discovery_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    discovered_via: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'manual'")
    )
    tenant_status: Mapped[TenantStatus] = mapped_column(
        _enum(TenantStatus, "tenant_status"), nullable=False, server_default=text("'candidate'")
    )
    transport_class: Mapped[TransportClass] = mapped_column(
        _enum(TransportClass, "transport_class"), nullable=False
    )
    connector: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_tier: Mapped[RefreshTier] = mapped_column(
        _enum(RefreshTier, "refresh_tier"), nullable=False, server_default=text("'warm'")
    )
    health: Mapped[SourceHealth] = mapped_column(
        _enum(SourceHealth, "source_health"), nullable=False, server_default=text("'healthy'")
    )
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    expected_min_jobs: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    backoff_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    robots_note: Mapped[str | None] = mapped_column(Text)
    tos_note: Mapped[str | None] = mapped_column(Text)
    rate_limit_rps: Mapped[Decimal] = mapped_column(
        Numeric(6, 3), nullable=False, server_default=text("0.5")
    )
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    platform: Mapped[Platform] = relationship(back_populates="sources")

    __table_args__ = (
        UniqueConstraint("platform_key", "tenant_key", name="source_tenant"),
        CheckConstraint(
            "NOT (config ?| ARRAY['token','password','secret','api_key','authorization'])",
            name="config_no_secrets",
        ),
        Index("source_due", "next_due_at", postgresql_where=text("enabled")),
    )


class JobCluster(Base):
    __tablename__ = "job_cluster"

    id: Mapped[uuid.UUID] = uuid_pk()
    canonical_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("job.id", name="job_cluster_canonical_job_id_fkey", use_alter=True),
        nullable=False,
        unique=True,
    )
    dedup_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class Job(Base):
    """One row per ingested posting, pre-dedup (`docs/schema.md` §5)."""

    __tablename__ = "job"

    id: Mapped[uuid.UUID] = uuid_pk()
    origin_source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source.id", ondelete="RESTRICT"), nullable=False
    )
    origin_external_id: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    company: Mapped[str] = mapped_column(Text, nullable=False)
    company_normalized: Mapped[str] = mapped_column(Text, nullable=False)
    company_domain: Mapped[str | None] = mapped_column(Text)
    apply_host: Mapped[str | None] = mapped_column(Text)
    description_html: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    description_text: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    content_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    city_raw: Mapped[str | None] = mapped_column(Text)
    city_key: Mapped[str | None] = mapped_column(Text)
    region_raw: Mapped[str | None] = mapped_column(Text)
    region_key: Mapped[str | None] = mapped_column(Text)
    country_code: Mapped[str | None] = mapped_column(Text)
    location_raw: Mapped[str | None] = mapped_column(Text)
    remote_status: Mapped[RemoteStatus] = mapped_column(
        _enum(RemoteStatus, "remote_status"), nullable=False, server_default=text("'unknown'")
    )
    employment_type: Mapped[EmploymentType] = mapped_column(
        _enum(EmploymentType, "employment_type"), nullable=False, server_default=text("'unknown'")
    )
    salary_min: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    salary_max: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    salary_currency: Mapped[str | None] = mapped_column(Text)
    salary_period: Mapped[SalaryPeriod] = mapped_column(
        _enum(SalaryPeriod, "salary_period"), nullable=False, server_default=text("'unknown'")
    )
    salary_raw: Mapped[str | None] = mapped_column(Text)
    salary_is_estimated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[JobStatus] = mapped_column(
        _enum(JobStatus, "job_status"), nullable=False, server_default=text("'unverified'")
    )
    stale_since_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closure_evidence_kind: Mapped[ClosureEvidence | None] = mapped_column(
        _enum(ClosureEvidence, "closure_evidence"), nullable=True
    )
    closure_evidence_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cluster_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "job_cluster.id", name="job_cluster_id_fkey", use_alter=True, ondelete="SET NULL"
        ),
    )
    is_canonical: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    merged_into_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("job.id", ondelete="SET NULL")
    )
    sort_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        Computed("coalesce(posted_at, first_seen_at)", persisted=True),
        nullable=False,
    )
    search_tsv: Mapped[str] = mapped_column(TSVECTOR, Computed(SEARCH_TSV_SQL, persisted=True))
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    occurrences: Mapped[list[JobOccurrence]] = relationship(back_populates="job")

    __table_args__ = (
        UniqueConstraint("origin_source_id", "origin_external_id", name="job_origin_identity"),
        CheckConstraint(
            "salary_min IS NULL OR salary_max IS NULL OR salary_min <= salary_max",
            name="salary_range_valid",
        ),
        CheckConstraint(
            "(salary_min IS NULL AND salary_max IS NULL) OR salary_currency IS NOT NULL",
            name="salary_currency_required",
        ),
        CheckConstraint(
            "status <> 'closed' OR (closure_evidence_kind IS NOT NULL"
            " AND closure_evidence_at IS NOT NULL)",
            name="closed_requires_evidence",
        ),
        CheckConstraint(
            "status <> 'open' OR (closure_evidence_kind IS NULL AND stale_since_at IS NULL)",
            name="open_forbids_evidence",
        ),
        CheckConstraint(
            "status <> 'stale' OR stale_since_at IS NOT NULL",
            name="stale_requires_since",
        ),
        CheckConstraint(
            "merged_into_job_id IS NULL OR cluster_id IS NOT NULL", name="merged_requires_cluster"
        ),
        CheckConstraint(
            "NOT (is_canonical AND merged_into_job_id IS NOT NULL)",
            name="canonical_cannot_be_merged",
        ),
        CheckConstraint(
            "country_code IS NULL OR country_code ~ '^[A-Z]{2}$'", name="country_code_iso2"
        ),
        CheckConstraint(
            "salary_currency IS NULL OR salary_currency ~ '^[A-Z]{3}$'",
            name="salary_currency_iso4217",
        ),
        Index(
            "job_one_canonical_per_cluster",
            "cluster_id",
            unique=True,
            postgresql_where=text("is_canonical AND cluster_id IS NOT NULL"),
        ),
        Index("job_open_liveness", "id", postgresql_where=text("status = 'open'")),
    )


class JobOccurrence(Base):
    """Where a posting was seen. Never deleted, never merged (`docs/schema.md` §6)."""

    __tablename__ = "job_occurrence"

    id: Mapped[uuid.UUID] = uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("job.id", ondelete="RESTRICT"), nullable=False
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source.id", ondelete="RESTRICT"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    apply_urls: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    apply_url: Mapped[str | None] = mapped_column(
        Text, Computed("apply_urls[1]", persisted=True)
    )
    title_as_posted: Mapped[str] = mapped_column(Text, nullable=False)
    location_as_posted: Mapped[str | None] = mapped_column(Text)
    salary_raw_as_posted: Mapped[str | None] = mapped_column(Text)
    raw_payload_ref: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_verification_status: Mapped[VerificationStatus | None] = mapped_column(
        _enum(VerificationStatus, "verification_status"), nullable=True
    )
    is_live: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    job: Mapped[Job] = relationship(back_populates="occurrences")

    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="occurrence_identity"),
        Index("occurrence_job_live", "job_id", postgresql_where=text("is_live")),
        Index("occurrence_source_stale", "source_id", "is_live", "last_seen_at"),
        Index("occurrence_next_check", "source_id", "last_seen_at"),
    )


class FetchRun(Base):
    """Written at claim, finalized at completion (`docs/schema.md` §7)."""

    __tablename__ = "fetch_run"

    id: Mapped[uuid.UUID] = uuid_pk()
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source.id", ondelete="RESTRICT"), nullable=False
    )
    # Deliberately NOT a foreign key: the queue is prunable, this is the audit record.
    queue_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[FetchOutcome | None] = mapped_column(_enum(FetchOutcome, "fetch_outcome"))
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_class: Mapped[ErrorClass] = mapped_column(
        _enum(ErrorClass, "error_class"), nullable=False, server_default=text("'none'")
    )
    error_detail: Mapped[str | None] = mapped_column(Text)
    jobs_found: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    jobs_new: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    jobs_updated: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    jobs_stale: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    jobs_closed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    pages_fetched: Mapped[int | None] = mapped_column(Integer)
    pages_expected: Mapped[int | None] = mapped_column(Integer)
    listing_complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    worker_id: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("fetch_run_source_time", "source_id", text("started_at DESC")),
        Index(
            "fetch_run_failures",
            "source_id",
            text("started_at DESC"),
            postgresql_where=text("outcome IN ('failure','abandoned')"),
        ),
        Index(
            "fetch_run_complete_listing",
            "source_id",
            text("started_at DESC"),
            postgresql_where=text("listing_complete"),
        ),
    )


class ChangeEvent(Base):
    __tablename__ = "change_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("job.id", ondelete="RESTRICT"), nullable=False
    )
    occurrence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("job_occurrence.id", ondelete="RESTRICT")
    )
    fetch_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fetch_run.id", ondelete="RESTRICT")
    )
    field: Mapped[str] = mapped_column(Text, nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "occurrence_id",
            "field",
            "new_value",
            "fetch_run_id",
            name="change_event_idempotent",
        ),
        Index("change_event_job_time", "job_id", text("detected_at DESC")),
        Index("change_event_run", "fetch_run_id"),
    )


__all__ = [
    "ChangeEvent",
    "FetchRun",
    "Job",
    "JobCluster",
    "JobOccurrence",
    "Platform",
    "Source",
]
