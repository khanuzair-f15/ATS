"""Connectors: fetch + parse only. See `jobindex.connectors.base`."""

from jobindex.connectors.base import (
    Completeness,
    Connector,
    ListingPage,
    RawPosting,
    Tenant,
    closure_eligible,
)

__all__ = [
    "Completeness",
    "Connector",
    "ListingPage",
    "RawPosting",
    "Tenant",
    "closure_eligible",
]
