"""Connectors: fetch + parse only. See `jobindex.connectors.base`."""

from jobindex.connectors.base import (
    Completeness,
    Connector,
    ListingPage,
    RawPosting,
    Tenant,
    closure_eligible,
)
from jobindex.connectors.context import (
    ConnectorConfig,
    ConnectorContext,
    LogContext,
    RateLimitConfig,
    RateLimiter,
    classify_error,
    create_connector_context,
    is_retryable_error,
    resolve_secret,
    setup_connector_logging,
)

__all__ = [
    "Completeness",
    "Connector",
    "ConnectorConfig",
    "ConnectorContext",
    "ListingPage",
    "LogContext",
    "RateLimitConfig",
    "RateLimiter",
    "RawPosting",
    "Tenant",
    "classify_error",
    "closure_eligible",
    "create_connector_context",
    "is_retryable_error",
    "resolve_secret",
    "setup_connector_logging",
]
