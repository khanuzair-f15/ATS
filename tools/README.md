# Tools for Connector Development

This directory contains utilities for capturing and replaying HTTP fixtures for offline connector testing.

## Tools

### capture.py

Capture live HTTP requests and responses as structured fixtures for offline testing.

**Usage:**
```bash
python -m tools.capture <url> --connector <key> --case <name> [options]
```

**Options:**
- `url` - URL to capture (required)
- `--connector` - Connector key, e.g., `greenhouse` (required)
- `--case` - Case name, e.g., `list_page_1` (required)
- `--method` - HTTP method (default: GET)
- `--header` - Request header (format: `Key: Value`), can be repeated
- `--body` - Request body string
- `--fixtures-dir` - Fixtures directory (default: `tests/fixtures`)
- `--dry-run` - Show what would be captured without making request

**Example:**
```bash
python -m tools.capture "https://boards-api.greenhouse.io/v1/boards/acme/jobs" \
    --connector greenhouse \
    --case list_page_1
```

**Output:**
Creates fixture directory structure:
```
tests/fixtures/greenhouse/list_page_1/
├── manifest.json    # Metadata (timestamp, UA, hashes, redaction status)
├── request.json     # Redacted request
└── response.json    # Redacted response
```

**Security:**
- Automatically redacts sensitive query parameters (token, key, secret, password, auth, access_token, refresh_token, api_key, client_secret, client_id, authorization)
- Redacts sensitive headers (Authorization, Cookie, Set-Cookie, X-Api-Key, etc.)
- Redacts sensitive JSON body fields (recursive, including nested objects)
- Generates SHA-256 integrity hashes
- Records redaction status in manifest

### replay.py

Replay captured fixtures without network access.

**Usage:**
```bash
python -m tools.replay validate [--fixtures-dir tests/fixtures]
python -m tools.replay list [--fixtures-dir tests/fixtures]
```

**Commands:**
- `validate` - Load and validate all fixtures (check SHA-256 integrity)
- `list` - List all available fixtures

**Programmatic Usage:**
```python
from tools.replay import ReplayTransport, FixtureLoader, create_replay_context

# Use with httpx
transport = ReplayTransport(fixtures_dir=Path("tests/fixtures"))
client = httpx.AsyncClient(transport=transport)
response = await client.get("https://api.example.com/jobs")

# Or use with ConnectorContext
client = await create_replay_context("greenhouse", "list_page_1")
```

**Features:**
- Request matching via normalized hashes (method, redacted URL, filtered headers, redacted body)
- Filters out auto-added headers (Host) for reliable matching
- Strict mode (raise on missing fixture) or non-strict mode (return 404)
- Default fixture fallback
- SHA-256 integrity validation on load

## Fixture Structure

```
tests/fixtures/
├── <platform_key>/
│   ├── <case_name>/
│   │   ├── manifest.json    # Metadata
│   │   ├── request.json     # Redacted request
│   │   └── response.json    # Redacted response
│   └── <another_case>/
│       ├── ...
└── <another_platform>/
    └── ...
```

### manifest.json
```json
{
  "format_version": "1.0",
  "platform_key": "greenhouse",
  "case_name": "list_page_1",
  "captured_at": "2024-01-01T00:00:00+00:00",
  "user_agent": "JobIndexBot/0.1 (+https://example.invalid/jobindex; contact=ops@example.invalid)",
  "request_hash": "sha256...",
  "response_hash": "sha256...",
  "redacted": true,
  "original_url": "https://api.example.com/jobs?token=secret",
  "method": "GET"
}
```

### request.json / response.json
Redacted request/response with sensitive data replaced by `***REDACTED***`.

## Redaction Rules

### Query Parameters (case-insensitive)
- `token`, `key`, `secret`, `password`, `auth`
- `access_token`, `refresh_token`, `api_key`
- `client_secret`, `client_id`, `authorization`

### Headers (case-insensitive)
- `Authorization`, `Cookie`, `Set-Cookie`
- `X-Api-Key`, `X-Auth-Token`, `Proxy-Authorization`
- `X-CSRF-Token`, `X-XSRF-Token`

### JSON Body Fields (recursive, case-insensitive)
Same as query parameters, applied recursively to nested objects and arrays.
Complex values (objects/arrays) under sensitive keys are recursively redacted rather than entirely replaced.

## Integration with ConnectorContext

The capture tool uses the shared Phase 1 `ConnectorContext` for HTTP operations, providing:
- HTTP/2 support
- Retry policies with exponential backoff
- Rate limiting (token bucket)
- Structured logging with correlation IDs
- Secret resolution per ADR 0005
- Secret redaction in logs

## Testing

Run all capture/replay tests:
```bash
python -m pytest tests/connectors/test_capture_replay.py -v
```

Run all tests:
```bash
python -m pytest -q
```