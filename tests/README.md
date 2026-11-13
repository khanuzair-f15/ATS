# Tests — Offline-First Test Suite

**Purpose**: Verifies JobIndex behavior without network or database dependencies (except `@pytest.mark.db` tests).

**Responsibilities**:
- Platform inventory integrity
- Connector Protocol conformance
- Schema parity (ORM vs `docs/schema.md`)
- API health endpoints
- Database constraints (requires PostgreSQL)

**Architecture**:
```
tests/
├── conftest.py                    # Offline fixtures, markers, shared utilities
├── test_health.py                 # /healthz, /readyz via dependency override
├── test_platform_inventory.py     # 78 platforms, class/wave sums, coverage=outstanding
├── test_connector_boundary.py     # Protocol conformance, Completeness, Tenant secret rejection
├── test_schema_parity.py          # ORM vs docs/schema.md (columns, types, constraints, indexes)
└── db/
    └── test_platform_seed.py      # @pytest.mark.db — row-for-row seed verification
```

**Key Design: Offline-First**
- `no_network` autouse fixture blocks non-localhost sockets
- Allows localhost for asyncio proactor (Windows)
- `@pytest.mark.db` tests skip the guard and require live PostgreSQL
- Zero network calls in standard test run

**Markers**:
| Marker | Description |
|--------|-------------|
| `db` | Requires live PostgreSQL (`JOBINDEX_DATABASE_URL`), skipped otherwise |

**Fixtures** (`conftest.py`):
- `no_network` (autouse) — Blocks `socket.connect`/`connect_ex`/`getaddrinfo` to non-localhost
- `event_loop_policy` — WindowsSelectorEventLoopPolicy on Windows
- `app` — FastAPI app instance
- `async_client` — `httpx.AsyncClient` with `ASGITransport`

**Test Categories**:

| Test File | Scope | Network | Database |
|-----------|-------|---------|----------|
| `test_health.py` | API endpoints | ❌ | ❌ (mocked) |
| `test_platform_inventory.py` | 78 platforms | ❌ | ❌ |
| `test_connector_boundary.py` | Protocol contract | ❌ | ❌ |
| `test_schema_parity.py` | ORM ↔ Schema.md | ❌ | ❌ |
| `db/test_platform_seed.py` | Seed verification | ❌ | ✅ (skipped if no DB) |

**Running Tests**:
```bash
# All offline tests (default)
pytest -q

# Including DB tests (requires PostgreSQL)
JOBINDEX_DATABASE_URL=postgresql+asyncpg://... pytest -q

# Specific test file
pytest tests/test_health.py -v

# With coverage
pytest --cov=jobindex --cov-report=term-missing
```

**Quality Gates**:
- `ruff check src tests` — Linting (line length 100, strict)
- `mypy src` — Strict type checking
- `pytest -q` — All tests pass, 0 network calls

**Phase 0 Verification**:
- `python scripts/verify_phase0.py` — Manifest ↔ inventory, class/wave sums, schema refs

**Current Implementation Status**: S1 complete — 33 tests pass (29 offline + 4 DB skipped), linting and type checking clean.

**Future Test Requirements** (planned):
| Step | Test Area | Description |
|------|-----------|-------------|
| S2 | Connector fixtures | Capture/replay, golden output comparison |
| S3 | Greenhouse E2E | Live ingestion, idempotency, fixture capture |
| S5 | Scheduler chaos | Forced 500s, backoff, zero closures |
| S6 | Invariant suite | Table-driven closure vs failure tests |
| S9 | Dedup evaluation | ≥400 true-duplicate pairs, Wilson 95% LB ≥ 0.95 |
| S14 | NFR benchmarks | Search p95 at 500k, refresh wall time |