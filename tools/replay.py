#!/usr/bin/env python3
"""Fixture replay transport for HTTPX.

Provides a transport that returns recorded responses from fixtures
without making network requests. Used for offline testing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import httpx

from jobindex.connectors.context import RedactingFilter

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

FIXTURE_FORMAT_VERSION = "1.0"
DEFAULT_FIXTURES_DIR = Path("tests/fixtures")

SENSITIVE_QUERY_PARAMS = {
    "token",
    "key",
    "secret",
    "password",
    "auth",
    "access_token",
    "refresh_token",
    "api_key",
    "client_secret",
    "client_id",
    "authorization",
}

SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
    "proxy-authorization",
    "x-csrf-token",
    "x-xsrf-token",
}


# -----------------------------------------------------------------------------
# Redaction utilities (must match capture.py exactly)
# -----------------------------------------------------------------------------


def redact_query_params(url: str) -> str:
    """Redact sensitive query parameters from a URL."""
    parsed = urlparse(url)
    if not parsed.query:
        return url

    query_params = parse_qs(parsed.query, keep_blank_values=True)
    parts = []
    for key, values in query_params.items():
        if key.lower() in SENSITIVE_QUERY_PARAMS:
            for _ in values:
                parts.append(f"{key}=***REDACTED***")
        else:
            for value in values:
                parts.append(f"{key}={value}")

    new_query = "&".join(parts)
    return urlunparse(parsed._replace(query=new_query))


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Redact sensitive headers."""
    redacted = {}
    for key, value in headers.items():
        if key.lower() in SENSITIVE_HEADERS:
            redacted[key] = "***REDACTED***"
        else:
            redacted[key] = value
    return redacted


# -----------------------------------------------------------------------------
# Request matching
# -----------------------------------------------------------------------------


def normalize_request(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    content: bytes | str | None = None,
) -> dict[str, Any]:
    """Normalize a request for comparison with fixture."""
    # Filter out headers that are automatically added by HTTP clients
    filtered_headers = {k: v for k, v in (headers or {}).items() if k.lower() != "host"}
    return {
        "method": method.upper(),
        "url": redact_query_params(url),
        "headers": redact_headers(filtered_headers),
        "body": _redact_body(content) if content else None,
    }


def _redact_body(content: bytes | str | None) -> Any:
    if content is None:
        return None
    if isinstance(content, bytes):
        try:
            content = content.decode("utf-8")
        except UnicodeDecodeError:
            return "<binary data>"
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return content
    return _redact_json_object(data)


def _redact_json_object(obj: Any) -> Any:
    if isinstance(obj, dict):
        redacted = {}
        sensitive_keys = {"token", "key", "secret", "password", "auth", "access_token", "refresh_token", "api_key", "client_secret", "client_id", "authorization"}
        for key, value in obj.items():
            if key.lower() in sensitive_keys:
                # If value is a complex object, recurse into it instead of redacting entirely
                if isinstance(value, (dict, list)):
                    redacted[key] = _redact_json_object(value)
                else:
                    redacted[key] = "***REDACTED***"
            else:
                redacted[key] = _redact_json_object(value)
        return redacted
    elif isinstance(obj, list):
        return [_redact_json_object(item) for item in obj]
    else:
        return obj


def compute_request_hash(request: dict[str, Any]) -> str:
    """Compute hash of normalized request for matching."""
    normalized = json.dumps(request, sort_keys=True).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


# -----------------------------------------------------------------------------
# Fixture loading and validation
# -----------------------------------------------------------------------------


class FixtureNotFoundError(Exception):
    """Raised when no matching fixture is found."""

    def __init__(self, method: str, url: str, fixture_dir: Path):
        self.method = method
        self.url = url
        self.fixture_dir = fixture_dir
        super().__init__(f"No fixture found for {method} {url} in {fixture_dir}")


class FixtureIntegrityError(Exception):
    """Raised when fixture integrity check fails."""

    pass


class FixtureLoader:
    """Loads and manages HTTP fixtures for replay."""

    def __init__(self, fixtures_dir: Path = DEFAULT_FIXTURES_DIR):
        self.fixtures_dir = fixtures_dir
        self._fixtures: dict[str, dict[str, Any]] = {}
        self._load_all()

    def _load_all(self) -> None:
        """Load all fixtures from the fixtures directory."""
        if not self.fixtures_dir.exists():
            return

        for platform_dir in self.fixtures_dir.iterdir():
            if not platform_dir.is_dir():
                continue
            for case_dir in platform_dir.iterdir():
                if not case_dir.is_dir():
                    continue

                manifest_path = case_dir / "manifest.json"
                request_path = case_dir / "request.json"
                response_path = case_dir / "response.json"

                if not all(p.exists() for p in (manifest_path, request_path, response_path)):
                    continue

                try:
                    manifest = json.loads(manifest_path.read_text())
                    request = json.loads(request_path.read_text())
                    response = json.loads(response_path.read_text())

                    # Verify integrity
                    request_hash = hashlib.sha256(
                        json.dumps(request, sort_keys=True).encode("utf-8")
                    ).hexdigest()
                    response_hash = hashlib.sha256(
                        json.dumps(response, sort_keys=True).encode("utf-8")
                    ).hexdigest()

                    if manifest.get("request_hash") != request_hash:
                        raise FixtureIntegrityError(
                            f"Request hash mismatch in {case_dir}: "
                            f"expected {manifest['request_hash']}, got {request_hash}"
                        )
                    if manifest.get("response_hash") != response_hash:
                        raise FixtureIntegrityError(
                            f"Response hash mismatch in {case_dir}: "
                            f"expected {manifest['response_hash']}, got {response_hash}"
                        )

                    key = f"{manifest['platform_key']}/{manifest['case_name']}"
                    self._fixtures[key] = {
                        "manifest": manifest,
                        "request": request,
                        "response": response,
                        "request_hash": compute_request_hash(request),
                    }
                except (json.JSONDecodeError, KeyError) as e:
                    print(f"Warning: Failed to load fixture {case_dir}: {e}")

    def get_fixture(
        self,
        platform_key: str,
        case_name: str,
    ) -> dict[str, Any] | None:
        """Get a specific fixture by platform and case name."""
        key = f"{platform_key}/{case_name}"
        return self._fixtures.get(key)

    def find_matching_fixture(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        content: bytes | str | None = None,
    ) -> dict[str, Any] | None:
        """Find a fixture matching the given request."""
        normalized = normalize_request(method, url, headers, content)
        request_hash = compute_request_hash(normalized)

        for fixture in self._fixtures.values():
            if fixture["request_hash"] == request_hash:
                return fixture

        return None

    def list_fixtures(self) -> list[dict[str, str]]:
        """List all available fixtures."""
        return [
            {
                "platform_key": f["manifest"]["platform_key"],
                "case_name": f["manifest"]["case_name"],
                "captured_at": f["manifest"]["captured_at"],
            }
            for f in self._fixtures.values()
        ]


# -----------------------------------------------------------------------------
# Replay transport
# -----------------------------------------------------------------------------


class ReplayTransport(httpx.BaseTransport):
    """HTTPX transport that returns recorded responses from fixtures."""

    def __init__(
        self,
        fixtures_dir: Path = DEFAULT_FIXTURES_DIR,
        strict: bool = True,
        default_fixture: dict[str, Any] | None = None,
    ):
        self.loader = FixtureLoader(fixtures_dir)
        self.strict = strict
        self.default_fixture = default_fixture

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Handle a request by returning a matching fixture response."""
        # Read request body
        content = request.content
        if content is None and hasattr(request, "_content"):
            content = request._content

        # Find matching fixture
        fixture = self.loader.find_matching_fixture(
            method=request.method,
            url=str(request.url),
            headers=dict(request.headers),
            content=content,
        )

        if fixture is None:
            if self.default_fixture:
                fixture = self.default_fixture
            elif self.strict:
                raise FixtureNotFoundError(
                    request.method, str(request.url), self.loader.fixtures_dir
                )
            else:
                # Return a 404 response for non-strict mode
                return httpx.Response(
                    status_code=404,
                    content=b"Fixture not found",
                    request=request,
                )

        response_data = fixture["response"]
        return httpx.Response(
            status_code=response_data.get("status_code", 200),
            headers=response_data.get("headers", {}),
            content=json.dumps(response_data.get("body", {})).encode("utf-8"),
            request=request,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Async version of handle_request."""
        return self.handle_request(request)


# -----------------------------------------------------------------------------
# Integration with ConnectorContext
# -----------------------------------------------------------------------------


def create_replay_context(
    platform_key: str,
    case_name: str,
    fixtures_dir: Path = DEFAULT_FIXTURES_DIR,
) -> httpx.AsyncClient:
    """Create an AsyncClient configured with replay transport for a specific fixture."""
    loader = FixtureLoader(fixtures_dir)
    fixture = loader.get_fixture(platform_key, case_name)

    if fixture is None:
        raise FixtureNotFoundError(
            "N/A", "N/A", fixtures_dir
        )

    transport = ReplayTransport(fixtures_dir=fixtures_dir, strict=True)
    return httpx.AsyncClient(transport=transport)


# -----------------------------------------------------------------------------
# CLI for fixture validation
# -----------------------------------------------------------------------------


def validate_fixtures(fixtures_dir: Path = DEFAULT_FIXTURES_DIR) -> int:
    """Validate all fixtures in the directory."""
    try:
        loader = FixtureLoader(fixtures_dir)
        fixtures = loader.list_fixtures()
        print(f"Loaded {len(fixtures)} fixtures:")
        for f in fixtures:
            print(f"  {f['platform_key']}/{f['case_name']} (captured: {f['captured_at']})")
        return 0
    except FixtureIntegrityError as e:
        print(f"Integrity error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Validation failed: {e}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tools.replay",
        description="Validate and manage HTTP replay fixtures",
    )
    parser.add_argument(
        "command",
        choices=["validate", "list"],
        help="Command to run",
    )
    parser.add_argument(
        "--fixtures-dir",
        type=Path,
        default=DEFAULT_FIXTURES_DIR,
        help=f"Fixtures directory (default: {DEFAULT_FIXTURES_DIR})",
    )
    return parser


def main() -> int:
    import sys

    parser = build_parser()
    args = parser.parse_args()

    if args.command == "validate":
        return validate_fixtures(args.fixtures_dir)
    elif args.command == "list":
        loader = FixtureLoader(args.fixtures_dir)
        fixtures = loader.list_fixtures()
        for f in fixtures:
            print(f"{f['platform_key']}/{f['case_name']}")
        return 0
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())