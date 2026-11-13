# jobindex.connectors — Connector Protocol and Base Types

**Purpose**: Defines the contract that all platform connectors must implement.

**Responsibilities**:
- `Connector` Protocol — Structural contract for fetch + parse
- `Tenant` — Connector input (tenant config, no secrets)
- `RawPosting` — Verbatim job data as posted
- `ListingPage` — Paginated listing results
- `Completeness` — Enum governing absence-based closure eligibility

**Architecture**:
```
src/jobindex/connectors/
├── __init__.py    # Exports Protocol and base types
└── base.py        # Protocol, dataclasses, enums
```

**Dependencies**:
- `jobindex.db.enums.TransportClass` — Transport classification
- Standard library only (dataclasses, enum, typing)

**Interfaces**:

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
```python
class Connector(Protocol):
    platform_key: str                    # e.g., "greenhouse"
    transport: TransportClass            # api_json | embedded_json | html | headless | board_api
    completeness: Completeness           # FULL | WINDOWED | UNKNOWN

    async def list_page(tenant: Tenant, cursor: str | None = None) -> ListingPage
    async def fetch_detail(tenant: Tenant, posting: RawPosting) -> RawPosting
```

**Connector Rules**:
1. Fetch + parse ONLY — no database access, no closure decisions, no dedup
2. Must declare `completeness` — only `FULL` allows `fetch_run.listing_complete = true`
3. One connector per platform, parameterized by `Tenant`
4. Secrets resolved by `source.id` at runtime (ADR 0005 §D), never in `Tenant.config`

**Transport Categories**:
| TransportClass | Description | Typical Approach |
|----------------|-------------|------------------|
| `api_json` | Documented public JSON API | Direct HTTP + JSON |
| `embedded_json` | JSON in HTML/XHR | HTML parse + JSON extract |
| `html` | Server-rendered HTML | DOM parsing (BeautifulSoup/lxml) |
| `headless` | Requires browser | Playwright (last resort) |
| `board_api` | Job-board software API | Shared engine across tenants |

**Adding a Connector** (S2+):
1. Create `connectors/<platform_key>.py` implementing `Connector` Protocol
2. Add fixtures in `tests/fixtures/<platform_key>/`
3. Add test in `tests/connectors/test_<platform_key>.py`
4. Run offline: `pytest tests/connectors/test_<platform_key>.py -q`

**Current Implementation Status**: S1 complete — Protocol and base types defined. No production connectors yet (S3+).

**Future Requirements** (S2):
- Shared HTTP client with retries, timeouts, UA, rate limiting
- Fixture capture/replay tool (`tools/capture.py`, `tools/replay.py`)
- Authentication abstraction (OAuth, token refresh, API keys)
- Browser context pooling for Playwright