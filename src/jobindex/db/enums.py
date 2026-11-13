"""Postgres enum types — one Python StrEnum per `docs/schema.md` §2 type.

`values_callable` makes SQLAlchemy emit the *values* rather than the member names, so
the Python names can be readable while the database keeps the documented values.
"""

from __future__ import annotations

from enum import StrEnum


class TransportClass(StrEnum):
    API_JSON = "api_json"
    EMBEDDED_JSON = "embedded_json"
    HTML = "html"
    HEADLESS = "headless"
    BOARD_API = "board_api"


class PlatformClass(StrEnum):
    C1 = "C1"
    C2 = "C2"
    C3 = "C3"
    C4 = "C4"


class CoverageState(StrEnum):
    IMPLEMENTED = "implemented"
    TESTED = "tested"
    VERIFIED = "verified"
    OUTSTANDING = "outstanding"


class RefreshTier(StrEnum):
    HOT = "hot"
    WARM = "warm"
    COOL = "cool"
    DORMANT = "dormant"


class SourceHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    OPEN = "open"
    HALF_OPEN = "half_open"
    DISABLED = "disabled"


class TenantStatus(StrEnum):
    CANDIDATE = "candidate"
    VALIDATED = "validated"
    REJECTED = "rejected"


class JobStatus(StrEnum):
    OPEN = "open"
    STALE = "stale"
    CLOSED = "closed"
    UNVERIFIED = "unverified"


class RemoteStatus(StrEnum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class EmploymentType(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    TEMPORARY = "temporary"
    INTERNSHIP = "internship"
    VOLUNTEER = "volunteer"
    PER_DIEM = "per_diem"
    UNKNOWN = "unknown"


class SalaryPeriod(StrEnum):
    YEAR = "year"
    MONTH = "month"
    WEEK = "week"
    DAY = "day"
    HOUR = "hour"
    UNKNOWN = "unknown"


class FetchOutcome(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILURE = "failure"
    ABANDONED = "abandoned"


class ErrorClass(StrEnum):
    NONE = "none"
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    RATE_LIMITED = "rate_limited"
    BLOCKED = "blocked"
    NOT_FOUND = "not_found"
    PARSE = "parse"


class VerificationStatus(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    ERROR = "error"


class ClosureEvidence(StrEnum):
    HTTP_404 = "http_404"
    HTTP_410 = "http_410"
    PAYLOAD_MARKER = "payload_marker"
    ABSENT_FROM_FULL_LISTING = "absent_from_full_listing"
    MANUAL = "manual"
