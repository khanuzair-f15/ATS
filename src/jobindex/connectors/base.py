"""Connector boundary.

A connector does exactly two things: fetch bytes and parse them into `RawPosting`s. It
does not touch the database, does not decide that a job closed, and does not dedupe.
Those decisions belong to the pipeline, where they are enforced by database constraints
(`docs/schema.md` §8, §11) rather than by every connector author remembering a rule.

The one thing a connector must declare about itself is `completeness`, because that is
the only input to absence-based closure:

    FULL      the connector enumerates every open posting, and can prove it did
    WINDOWED  it sees a recent slice (search results, a page cap, a feed of new roles)
    UNKNOWN   it cannot say

Only `FULL` may ever produce `fetch_run.listing_complete = true`. A windowed connector
that reports completeness is the bug this enum exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from jobindex.db.enums import TransportClass


class Completeness(StrEnum):
    FULL = "full"
    WINDOWED = "windowed"
    UNKNOWN = "unknown"


def closure_eligible(completeness: Completeness) -> bool:
    """Absence from a listing may close a job only when the listing was provably whole."""
    return completeness is Completeness.FULL


@dataclass(frozen=True, slots=True)
class Tenant:
    """The connector's input. One `source` row, minus everything it must not see."""

    tenant_key: str
    entry_url: str
    discovery_url: str | None = None
    config: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        forbidden = {"token", "password", "secret", "api_key", "authorization"}
        leaked = forbidden.intersection(self.config)
        if leaked:
            raise ValueError(
                f"tenant config must not carry secrets (found {sorted(leaked)}); "
                "resolve them by source.id instead (ADR 0005)"
            )


@dataclass(frozen=True, slots=True)
class RawPosting:
    """Verbatim as posted. Normalization happens later and never overwrites these."""

    external_id: str
    title: str
    source_url: str
    apply_urls: tuple[str, ...]
    description_html: str = ""
    location_raw: str | None = None
    salary_raw: str | None = None
    posted_at_raw: str | None = None
    raw: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ListingPage:
    postings: tuple[RawPosting, ...]
    next_cursor: str | None = None
    is_last: bool = False


@runtime_checkable
class Connector(Protocol):
    """Structural contract every `connectors/<platform>.py` must satisfy."""

    platform_key: str
    transport: TransportClass
    completeness: Completeness

    async def list_page(self, tenant: Tenant, cursor: str | None = None) -> ListingPage:
        """Return one page of the tenant's listing."""
        ...

    async def fetch_detail(self, tenant: Tenant, posting: RawPosting) -> RawPosting:
        """Return the posting with detail fields filled in."""
        ...


__all__ = [
    "Completeness",
    "Connector",
    "ListingPage",
    "RawPosting",
    "Tenant",
    "closure_eligible",
]
