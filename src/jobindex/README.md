# jobindex package

**Purpose**: Main package for the JobIndex job indexing platform.

**Responsibilities**:
- Package initialization and version management
- CLI entry point (`jobindex` command)
- Platform registry (`jobindex.platforms` module)
- Configuration management (`jobindex.settings` module)

**Architecture**:
```
src/jobindex/
├── __init__.py          # Package exports, version
├── cli.py               # Command-line interface
├── platforms.py         # Platform registry (loads from docs/platforms.md)
├── settings.py          # Pydantic settings (env vars, .env)
├── api/                 # FastAPI application (read API)
├── connectors/          # Connector Protocol and base classes
├── db/                  # SQLAlchemy models, session, migrations
├── normalize/           # Normalization functions (future: S9)
├── scheduler/           # Queue scheduler (future: S5)
├── dedup/               # Deduplication engine (future: S9)
└── web/                 # Server-rendered UI (future: S11+)
```

**Dependencies**:
- Python 3.12+
- FastAPI, SQLAlchemy 2.x, asyncpg, Alembic, Pydantic, Pydantic-Settings
- Development: pytest, pytest-asyncio, httpx, ruff, mypy

**Interfaces**:
- `jobindex.platforms.PLATFORMS` — tuple of 78 `PlatformRow` objects
- `jobindex.settings.get_settings()` — cached settings instance
- `jobindex.cli.main()` — CLI entry point

**Configuration**:
- `JOBINDEX_DATABASE_URL` — PostgreSQL connection string
- `JOBINDEX_USER_AGENT` — Identifiable User-Agent for outbound requests
- `JOBINDEX_SECRET_DIR` — Optional directory for per-source secret files
- `JOBINDEX_LOG_LEVEL` — Logging level (default: INFO)

**Execution**:
```bash
# List platforms
python -m jobindex.cli platforms

# Run API server
uvicorn jobindex.api.app:app --reload
```

**Testing**:
- Offline tests: `pytest tests/ -q` (no network, no database)
- Database tests: `pytest tests/db/ -q` (requires PostgreSQL, `@pytest.mark.db`)

**Failure Handling**:
- Configuration errors raise `ValueError` at startup
- Missing `JOBINDEX_USER_AGENT` is a config error
- Platform key collisions raise at import time

**Current Implementation Status**: S1 complete — scaffold, fixture harness, first green test.