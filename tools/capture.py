#!/usr/bin/env python3
"""Fixture capture tool for connector HTTP interactions.

Captures live HTTP requests/responses into a structured fixture directory
for offline testing and replay.

Usage:
    python -m tools.capture <url> --connector <key> --case <name> [options]

Example:
    python -m tools.capture "https://boards-api.greenhouse.io/v1/boards/acme/jobs" \
        --connector greenhouse --case list_page_1
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import httpx

from jobindex.connectors.context import (
    ConnectorConfig,
    ConnectorContext,
    RedactingFilter,
)
from jobindex.connectors.base import Tenant
from jobindex.settings import get_settings

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
# Redaction utilities
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


def redact_json_body(body: bytes | str | None) -> Any:
    """Redact sensitive fields from JSON body."""
    if body is None:
        return None

    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError:
            return "<binary data>"

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body

    return _redact_json_object(data)


def _redact_json_object(obj: Any) -> Any:
    """Recursively redact sensitive fields in JSON object."""
    if isinstance(obj, dict):
        redacted = {}
        for key, value in obj.items():
            if key.lower() in SENSITIVE_QUERY_PARAMS:
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


def compute_sha256(data: bytes) -> str:
    """Compute SHA-256 hash of data."""
    return hashlib.sha256(data).hexdigest()


# -----------------------------------------------------------------------------
# Capture logic
# -----------------------------------------------------------------------------


async def capture_request(
    url: str,
    connector_key: str,
    case_name: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    fixtures_dir: Path = DEFAULT_FIXTURES_DIR,
) -> Path:
    """Capture a live HTTP request and save as fixture."""

    # Create fixture directory
    fixture_dir = fixtures_dir / connector_key / case_name
    fixture_dir.mkdir(parents=True, exist_ok=True)

    # Build request headers
    request_headers = dict(headers or {})
    settings = get_settings()
    request_headers.setdefault("User-Agent", settings.require_user_agent())
    request_headers.setdefault("Accept", "application/json, text/html;q=0.9, */*;q=0.8")

    # Capture request
    request_data = {
        "method": method.upper(),
        "url": redact_query_params(url),
        "headers": redact_headers(request_headers),
        "body": redact_json_body(body) if body else None,
    }

    # Make the actual request
    tenant = Tenant(
        tenant_key="capture",
        entry_url=url,
        discovery_url=url,
    )

    config = ConnectorConfig(
        platform_key=connector_key,
        tenant=tenant,
        source_id="capture",
        timeout_s=30.0,
    )

    response_data = None
    response_body = b""

    async with ConnectorContext(config) as ctx:
        try:
            response = await ctx.client.request(method, url, headers=request_headers, content=body)
            response_body = await response.aread()

            response_data = {
                "status_code": response.status_code,
                "headers": redact_headers(dict(response.headers)),
                "body": redact_json_body(response_body),
            }
        except Exception as e:
            # Capture error details
            response_data = {
                "status_code": 0,
                "headers": {},
                "body": {"error": str(e), "type": type(e).__name__},
            }
            raise

    # Compute hashes
    request_bytes = json.dumps(request_data, sort_keys=True).encode("utf-8")
    response_bytes = json.dumps(response_data, sort_keys=True).encode("utf-8")
    request_hash = compute_sha256(request_bytes)
    response_hash = compute_sha256(response_bytes)

    # Create manifest
    manifest = {
        "format_version": FIXTURE_FORMAT_VERSION,
        "platform_key": connector_key,
        "case_name": case_name,
        "captured_at": datetime.now(UTC).isoformat(),
        "user_agent": request_headers.get("User-Agent", ""),
        "request_hash": request_hash,
        "response_hash": response_hash,
        "redacted": True,
        "original_url": url,
        "method": method.upper(),
    }

    # Write fixture files
    (fixture_dir / "request.json").write_text(json.dumps(request_data, indent=2))
    (fixture_dir / "response.json").write_text(json.dumps(response_data, indent=2))
    (fixture_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    return fixture_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tools.capture",
        description="Capture live HTTP request/response as fixture for offline testing",
    )
    parser.add_argument("url", help="URL to capture")
    parser.add_argument("--connector", required=True, help="Connector key (e.g., greenhouse)")
    parser.add_argument("--case", required=True, help="Case name (e.g., list_page_1)")
    parser.add_argument("--method", default="GET", help="HTTP method (default: GET)")
    parser.add_argument("--header", action="append", help="Request header (format: Key: Value)")
    parser.add_argument("--body", help="Request body (string)")
    parser.add_argument(
        "--fixtures-dir",
        type=Path,
        default=DEFAULT_FIXTURES_DIR,
        help=f"Fixtures directory (default: {DEFAULT_FIXTURES_DIR})",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would be captured without making request")
    return parser


async def main_async(args: argparse.Namespace) -> int:
    if args.dry_run:
        print(f"Would capture: {args.method} {args.url}")
        print(f"  Connector: {args.connector}")
        print(f"  Case: {args.case}")
        print(f"  Fixtures dir: {args.fixtures_dir}")
        return 0

    # Parse headers
    headers = {}
    for h in args.header or []:
        if ":" not in h:
            print(f"Invalid header format: {h}", file=sys.stderr)
            return 1
        key, value = h.split(":", 1)
        headers[key.strip()] = value.strip()

    # Parse body
    body = args.body.encode("utf-8") if args.body else None

    try:
        fixture_dir = await capture_request(
            url=args.url,
            connector_key=args.connector,
            case_name=args.case,
            method=args.method,
            headers=headers,
            body=body,
            fixtures_dir=args.fixtures_dir,
        )
        print(f"Fixture captured to: {fixture_dir}")
        return 0
    except Exception as e:
        print(f"Capture failed: {e}", file=sys.stderr)
        return 1


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())