# HTTP Test Fixtures

This directory contains captured HTTP request/response fixtures for offline connector testing.

## Structure

```
tests/fixtures/
├── <platform_key>/
│   ├── <case_name>/
│   │   ├── manifest.json    # Metadata (timestamp, UA, hashes, redaction status)
│   │   ├── request.json     # Redacted request
│   │   └── response.json    # Redacted response
│   └── ...
└── ...
```

## Fixture Format

Each case directory contains three files:

### manifest.json
Metadata about the capture:
- `format_version` - Fixture format version
- `platform_key` - Connector platform key (e.g., `greenhouse`)
- `case_name` - Descriptive case name (e.g., `list_page_1`)
- `captured_at` - ISO 8601 UTC timestamp
- `user_agent` - User-Agent used for capture
- `request_hash` - SHA-256 of request.json
- `response_hash` - SHA-256 of response.json
- `redacted` - Boolean, whether redaction was applied
- `original_url` - Original URL before redaction
- `method` - HTTP method

### request.json
Captured request with sensitive data redacted:
```json
{
  "method": "GET",
  "url": "https://api.example.com/jobs?token=***REDACTED***",
  "headers": {},
  "body": null
}
```

### response.json
Captured response with sensitive data redacted:
```json
{
  "status_code": 200,
  "headers": {"Content-Type": "application/json"},
  "body": {"jobs": [{"id": "1", "title": "Engineer"}]}
}
```

## Adding Fixtures

### Automatic Capture (Recommended)
Use the capture tool:
```bash
python -m tools.capture "https://api.example.com/jobs" \
    --connector greenhouse \
    --case list_page_1
```

### Manual Creation
1. Create directory: `tests/fixtures/<platform_key>/<case_name>/`
2. Create `request.json`, `response.json`, `manifest.json`
3. Run validation: `python -m tools.replay validate`

## Validation

Validate all fixtures:
```bash
python -m tools.replay validate
```

List all fixtures:
```bash
python -m tools.replay list
```

## Redaction

Fixtures must have sensitive data redacted:
- Query parameters: `token`, `key`, `secret`, `password`, `auth`, `access_token`, `refresh_token`, `api_key`, `client_secret`, `client_id`, `authorization`
- Headers: `Authorization`, `Cookie`, `Set-Cookie`, `X-Api-Key`, `X-Auth-Token`, `Proxy-Authorization`
- JSON body fields (recursive): same as query parameters

## Integrity

Each fixture includes SHA-256 hashes in the manifest for integrity verification. The replay loader validates these on load.

## Usage in Tests

```python
from tools.replay import ReplayTransport, create_replay_context
import httpx

# Option 1: Direct transport
transport = ReplayTransport(fixtures_dir=Path("tests/fixtures"))
client = httpx.AsyncClient(transport=transport)
response = await client.get("https://api.example.com/jobs")

# Option 2: ConnectorContext integration
client = await create_replay_context("greenhouse", "list_page_1")
response = await client.get("https://api.example.com/jobs")
```