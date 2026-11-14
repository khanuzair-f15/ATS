# jobindex.connectors — Connector Protocol, SDK, and Shared Infrastructure

**Purpose**: Defines the contract that all platform connectors must implement, plus the shared
execution infrastructure (HTTP client, retries, rate limiting, error classification, logging)
that makes writing 78 connectors maintainable.

**Responsibilities**:
- `Connector` Protocol — Structural contract for fetch + parse
- `Tenant` — Connector input (tenant config, no secrets)
- `RawPosting` — Verbatim job data as posted
- `ListingPage` — Paginated listing results
- `Completeness` — Enum governing absence-based closure eligibility
- `ConnectorContext` — Shared HTTP client with retries, rate limits, logging, secret resolution
- `RateLimiter` — Token-bucket rate limiting with per-host concurrency control
- `classify_error` — Centralized error classification to `ErrorClass` enum
- Structured logging with correlation IDs and secret redaction

## Architecture

```
src/jobindex/connectors/
├── __init__.py         # Exports all public APIs
├── base.py             # Protocol, dataclasses, enums (S1)
├── context.py          # ConnectorContext, RateLimiter, error classification, logging (S2)
├── errors.py           # Error classification (re-exported from context)
└── _template/          # Runnable reference connector (S2+)
    ├── __init__.py
    ├── connector.py
    └── README.md
```

## Dependencies

- `jobindex.db.enums.TransportClass` — Transport classification
- `jobindex.db.enums.ErrorClass` — Error classification
- `httpx` — Async HTTP client
- `tenacity` — Retry policies
- Standard library (dataclasses, enum, typing, asyncio, logging, uuid, urllib)

## Interfaces

### `Completeness` (StrEnum)

| Value | Meaning | Closure Eligible |
|-------|---------|------------------|
| `FULL` | Enumerates all open postings, can prove it | ✅ Yes |
| `WINDOWED` | Sees recent slice (search, page cap, feed) | ❌ No |
| `UNKNOWN` | Cannot say | ❌ No |

### `Tenant` (frozen dataclass)

```python
Tenant(
    tenant_key: str,           # e.g., "acme-corp"
    entry_url: str,            # Human-facing careers URL
    discovery_url: str | None, # Resolved machine endpoint
    config: dict[str, object]  # Non-secret parameters only
)
```

**Secret Rejection**: `config` must not contain `token`, `password`, `secret`, `api_key`, `authorization`. Raises `ValueError` if violated (ADR 0005 §D).

### `RawPosting` (frozen dataclass)

Verbatim job data — normalization happens later, never overwrites these.

```python
RawPosting(
    external_id: str,              # Platform's posting ID
    title: str,                    # As posted
    source_url: str,               # Listing URL
    apply_urls: tuple[str, ...],   # ALL apply targets
    description_html: str = "",    # Raw HTML
    location_raw: str | None,      # As posted
    salary_raw: str | None,        # As posted
    posted_at_raw: str | None,     # As posted
    raw: dict[str, object] = {}    # Extensible passthrough
)
```

### `ListingPage` (frozen dataclass)

```python
ListingPage(
    postings: tuple[RawPosting, ...],
    next_cursor: str | None = None,
    is_last: bool = False
)
```

### `Connector` (Protocol)

Structural contract every `connectors/<platform>.py` must satisfy.

```python
class Connector(Protocol):
    platform_key: str                    # e.g., "greenhouse"
    transport: TransportClass            # api_json | embedded_json | html | headless | board_api
    completeness: Completeness           # FULL | WINDOWED | UNKNOWN

    async def list_page(tenant: Tenant, cursor: str | None = None) -> ListingPage
    async def fetch_detail(tenant: Tenant, posting: RawPosting) -> RawPosting
```

### `ConnectorContext` (S2)

Async context manager providing a shared HTTP client with all cross-cutting concerns handled.

```python
from jobindex.connectors import create_connector_context, ConnectorConfig
from jobindex.connectors.base import Tenant

tenant = Tenant(tenant_key="acme", entry_url="https://acme.example.com/careers")
config = ConnectorConfig(
    platform_key="greenhouse",
    tenant=tenant,
    source_id="src-uuid",
    rate_limit_config=RateLimitConfig(requests_per_second=1.0, max_concurrency=2),
    max_retries=3,
    timeout_s=20.0,
)

async with create_connector_context(...) as ctx:
    response = await ctx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs")
    # Automatic: retries, rate limiting, logging, error classification
```

**Features**:
- **HTTP Client Lifecycle**: Manages `httpx.AsyncClient` with connection pooling, HTTP/2, timeouts
- **Retry Policy**: 3 attempts (configurable), exponential backoff with jitter, retries transient/rate-limited errors only
- **Rate Limiting**: Token bucket per host, respects `source.rate_limit_rps` and `max_concurrency`, jittered delays
- **Error Classification**: Maps exceptions/HTTP status to `ErrorClass` (transient, permanent, rate_limited, blocked, not_found, parse)
- **Structured Logging**: Correlation IDs, platform/tenant/source context, duration, status, error class
- **Secret Resolution**: Per ADR 0005 — from `JOBINDEX_SECRET_<source_id>` env var or `$JOBINDEX_SECRET_DIR/<source_id>` file
- **Secret Redaction**: Never logs Authorization, Cookie, Set-Cookie, X-Api-Key, or sensitive query parameters

#### Configuration

| Parameter | Source | Default |
|-----------|--------|---------|
| `http_timeout_s` | `Settings.http_timeout_s` | 20.0s |
| `max_concurrency_per_host` | `Settings.max_concurrency_per_host` | 1 |
| `request_jitter_ms` | `Settings.request_jitter_ms` | 250ms |
| `user_agent` | `Settings.user_agent` | Required, identifiable |
| `rate_limit_rps` | `source.rate_limit_rps` | 0.5 |
| `max_concurrency` | `source.max_concurrency` | 1 |

#### Methods

```python
async with ConnectorContext(config) as ctx:
    # GET with automatic retries, rate limiting, logging
    response = await ctx.get(url, operation="list_page")

    # POST with same guarantees
    response = await ctx.post(url, json=data, operation="fetch_detail")

    # Low-level request
    response = await ctx.request("PUT", url, operation="update")

    # Create child log context for nested operations
    op_ctx = ctx.with_operation("fetch_detail", attempt=2)
```

### `RateLimiter` (S2)

Global rate limiter managing per-host token buckets and concurrency semaphores.

```python
from jobindex.connectors import RateLimiter, RateLimitConfig

limiter = RateLimiter()
config = RateLimitConfig(requests_per_second=2.0, max_concurrency=3, burst_allowance=4)

async with limiter.acquire("https://api.example.com/jobs", config):
    # Make request - rate limited and concurrency controlled
    pass
```

### `classify_error` (S2)

Centralized error classification mapping exceptions and HTTP responses to `ErrorClass`.

```python
from jobindex.connectors import classify_error, is_retryable_error
from jobindex.db.enums import ErrorClass
import httpx

# From exception
error_class = classify_error(httpx.TimeoutException("timeout"))
# ErrorClass.TRANSIENT

# From HTTP response
error_class = classify_error(None, response)  # response.status_code = 429
# ErrorClass.RATE_LIMITED

# Check if retryable
if is_retryable_error(error_class):
    # retry logic
    pass
```

**Classification Rules**:

| Condition | ErrorClass | Retryable |
|-----------|------------|-----------|
| HTTP 2xx | NONE | No |
| HTTP 429 | RATE_LIMITED | Yes |
| HTTP 401, 403 | BLOCKED | No |
| HTTP 404, 410 | NOT_FOUND | No |
| HTTP 4xx (other) | PERMANENT | No |
| HTTP 5xx | TRANSIENT | Yes |
| `httpx.TimeoutException` | TRANSIENT | Yes |
| `httpx.NetworkError` | TRANSIENT | Yes |
| `httpx.ProtocolError` | PERMANENT | No |
| JSON decode error | PARSE | No |
| Unknown exception | TRANSIENT | Yes (safe default) |

### `LogContext` (S2)

Structured logging context carried through connector operations.

```python
ctx = LogContext(
    correlation_id="abc123",
    platform_key="greenhouse",
    tenant_key="acme",
    source_id="src-uuid",
    operation="list_page",
    attempt=1,
)
logger.info("Fetching page", extra=ctx.to_dict())
```

### Secret Resolution (ADR 0005)

```python
from jobindex.connectors import resolve_secret, inject_secret_headers

secret = resolve_secret("source-uuid")  # Checks env var then file
headers = inject_secret_headers({}, secret)  # Adds Authorization: Bearer <secret>
```

## Connector Rules

1. **Fetch + parse ONLY** — no database access, no closure decisions, no dedup
2. **Must declare `completeness`** — only `FULL` allows `fetch_run.listing_complete = true`
3. **One connector per platform**, parameterized by `Tenant`
4. **Secrets resolved by `source.id` at runtime** (ADR 0005 §D), never in `Tenant.config`
5. **Use `ConnectorContext`** for all HTTP operations — handles retries, rate limits, logging automatically

## Transport Categories

| TransportClass | Description | Typical Approach |
|----------------|-------------|------------------|
| `api_json` | Documented public JSON API | Direct HTTP + JSON |
| `embedded_json` | JSON in HTML/XHR | HTML parse + JSON extract |
| `html` | Server-rendered HTML | DOM parsing (selectolax/lxml) |
| `headless` | Requires browser | Playwright (last resort) |
| `board_api` | Job-board software API | Shared engine across tenants |

## Adding a Connector (S2+)

1. Create `connectors/<platform_key>.py` implementing `Connector` Protocol
2. Use `ConnectorContext` for all HTTP operations
3. Add fixtures in `tests/fixtures/<platform_key>/`
4. Add test in `tests/connectors/test_<platform_key>.py`
5. Run offline: `pytest tests/connectors/test_<platform_key>.py -q`

## Current Implementation Status

- **S1 complete** — Protocol and base types defined
- **S2 complete** — Shared HTTP client, retry, rate limiting, error classification, structured logging
- **S3+** — Production connectors (Greenhouse first)

## Future Requirements (S2 Phase 2+)

- Fixture capture/replay tool (`tools/capture.py`, `tools/replay.py`)
- HTML/JSON extraction utilities (`selectolax`, `lxml`, JSONPath)
- Authentication abstraction (OAuth, token refresh, API keys)
- Browser context pooling for Playwright
- Connector registry and auto-registration
- `jobindex.cli coverage` command