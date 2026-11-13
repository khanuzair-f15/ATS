# jobindex.api — FastAPI Read API

**Purpose**: Provides the HTTP API for the JobIndex platform.

**Responsibilities**:
- Liveness probe (`GET /healthz`)
- Readiness probe (`GET /readyz`)
- Future: Job search API (`GET /v1/jobs`, `GET /v1/jobs/{id}`)
- Future: Source/coverage API (`GET /v1/sources`, `GET /v1/coverage`)

**Architecture**:
```
src/jobindex/api/
├── __init__.py    # Exports app, create_app
└── app.py         # FastAPI application factory
```

**Dependencies**:
- FastAPI, Uvicorn
- `jobindex.db.session` — Database session and health check
- `jobindex.settings` — Configuration

**Interfaces**:
- `create_app()` — FastAPI application factory
- `get_db_probe()` — Dependency for injecting DB health check
- `app` — Pre-created FastAPI instance

**Configuration**:
- `JOBINDEX_LOG_LEVEL` — Logging level
- Database connectivity via `JOBINDEX_DATABASE_URL`

**Endpoints**:
| Method | Path | Description |
|--------|------|-------------|
| GET | `/healthz` | Liveness probe — never touches DB |
| GET | `/readyz` | Readiness probe — round-trips DB |

**Health Probe Contract**:
- `/healthz` returns `{"status": "ok", "version": "x.y.z"}` (200)
- `/readyz` returns `{"status": "ready", "database": "ok"}` (200) if DB reachable
- `/readyz` returns `{"status": "not_ready", "detail": "ErrorType"}` (503) if DB unreachable

**Execution**:
```bash
# Development
uvicorn jobindex.api.app:app --reload --port 8000

# Production (Vercel)
# Deployed as Python Function with vercel.json
```

**Testing**:
- `tests/test_health.py` — Verifies `/healthz` 200, `/readyz` 200/503 via dependency override
- Runs offline (no real DB) using FastAPI dependency overrides

**Failure Handling**:
- `/healthz` never fails due to DB issues (by design)
- `/readyz` fails fast with 503 on any DB connection error
- DB probe is injectable for testing

**Current Implementation Status**: S1 complete — only `/healthz` and `/readyz` implemented. Search API deferred to S4.