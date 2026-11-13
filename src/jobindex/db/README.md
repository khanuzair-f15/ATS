# jobindex.db — Database Layer

**Purpose**: SQLAlchemy 2.x async models, session management, and migration support for the JobIndex PostgreSQL database.

**Responsibilities**:
- Declarative base with explicit naming convention
- All S1 table models (7 tables, 14 enums)
- Async engine and session factory
- Health check for readiness probe
- Alembic migration support

**Architecture**:
```
src/jobindex/db/
├── __init__.py       # Exports Base, all models
├── base.py           # DeclarativeBase, naming convention, column helpers
├── enums.py          # 14 StrEnum classes matching PostgreSQL enums
├── models.py         # 7 table models (Platform, Source, Job, JobCluster, JobOccurrence, FetchRun, ChangeEvent)
├── session.py        # Async engine, session factory, health check
```

**Dependencies**:
- SQLAlchemy 2.x (async), asyncpg
- `jobindex.settings` — Database URL
- PostgreSQL 16 with extensions: `pgcrypto`, `pg_trgm`, `unaccent`

**Models** (S1 Baseline):

| Table | Purpose | Key Constraints |
|-------|---------|-----------------|
| `platform` | Registry of 78 integration targets | PK `key`, `family IN ('ats','board')`, `outstanding_requires_reason` |
| `source` | One row per tenant | PK `id`, `UNIQUE(platform_key, tenant_key)`, `config_no_secrets` |
| `job` | One row per ingested posting (pre-dedup) | PK `id`, `UNIQUE(origin_source_id, origin_external_id)`, generated `sort_at`, `search_tsv` |
| `job_cluster` | Dedup cluster authority | PK `id`, `UNIQUE(canonical_job_id) REFERENCES job(id)` |
| `job_occurrence` | Where a posting was seen (never deleted) | PK `id`, `UNIQUE(source_id, external_id)`, `is_live` guarded by deferred trigger |
| `fetch_run` | Audit of every fetch attempt | PK `id`, `listing_complete` = only closure gate |
| `change_event` | Audit log of field changes | PK `id`, `UNIQUE(occurrence_id, field, new_value, fetch_run_id)` |

**Circular FK Resolution** (`job` ↔ `job_cluster`):
1. `job` created with `cluster_id` (no FK yet)
2. `job_cluster` created with `canonical_job_id REFERENCES job(id)`
3. `ALTER TABLE job ADD CONSTRAINT job_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES job_cluster(id)`

**Deferred Triggers** (enforce invariants at commit):
| Trigger | Table | Purpose |
|---------|-------|---------|
| `job_first_seen_guard` | `job` (BEFORE UPDATE) | `first_seen_at` immutable |
| `occurrence_liveness_guard` | `job_occurrence` (AFTER INSERT/UPDATE) | Open job must have live occurrence |
| `job_status_liveness_guard` | `job` (AFTER INSERT/UPDATE) | Open job must have live occurrence |
| `cluster_has_canonical` | `job_cluster` (AFTER INSERT/UPDATE) | Canonical job must be in cluster and `is_canonical=true` |

**View**: `job_closure_candidates` — SQL-only closure predicate (stale > 72h + successful `listing_complete` fetch after `stale_since_at`)

**Enums** (14 PostgreSQL types):
- `transport_class`, `platform_class`, `coverage_state`, `refresh_tier`
- `source_health`, `tenant_status`, `job_status`, `remote_status`
- `employment_type`, `salary_period`, `fetch_outcome`, `error_class`
- `verification_status`, `closure_evidence`

**Interfaces**:
- `get_engine()` — Singleton async engine
- `get_session_factory()` — Singleton session factory
- `get_session()` — Async context manager for sessions
- `check_database()` — Round-trip DB health check
- `dispose_engine()` — Cleanup on shutdown

**Configuration**:
- `JOBINDEX_DATABASE_URL` — PostgreSQL connection string (asyncpg)

**Migrations**:
- Managed by Alembic (`migrations/` directory)
- Hand-written baseline (`0001_baseline.py`) — one statement per `op.execute()`
- Seed migration (`0002_seed_platforms.py`) — parses `docs/platforms.md`

**Testing**:
- Schema parity: `tests/test_schema_parity.py` — ORM vs `docs/schema.md`
- Seed verification: `tests/db/test_platform_seed.py` (`@pytest.mark.db`)
- Requires live PostgreSQL for DB tests

**Failure Handling**:
- Connection pool with `pool_pre_ping=True`
- `connect_args={"timeout": 3}` for fast failure
- Readiness probe fails fast on DB unreachable
- `config_no_secrets` CHECK prevents secret leakage

**Current Implementation Status**: S1 complete — all models, enums, migrations, session management. S5 adds `source_run_queue`, S9 adds dedup tables.

**Future Requirements** (S5+):
- `source_run_queue` table with `FOR UPDATE SKIP LOCKED` claim logic
- Queue indexes and reclaim procedure