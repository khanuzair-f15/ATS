"""Tools package for JobIndex connector development.

This package provides utilities for capturing and replaying HTTP fixtures
for offline connector testing.

Modules:
- capture: Capture live HTTP requests as fixtures
- replay: Replay fixtures without network access
"""

from tools.capture import capture_request, redact_query_params, redact_headers
from tools.replay import (
    FixtureLoader,
    ReplayTransport,
    FixtureNotFoundError,
    FixtureIntegrityError,
    create_replay_context,
)

__all__ = [
    "capture_request",
    "redact_query_params",
    "redact_headers",
    "FixtureLoader",
    "ReplayTransport",
    "FixtureNotFoundError",
    "FixtureIntegrityError",
    "create_replay_context",
]