# Documentation — Architecture Decisions, Schema, and Platform Inventory

**Purpose**: Authoritative source of truth for JobIndex architecture, data model, and platform scope.

**Responsibilities**:
- Architecture Decision Records (ADRs)
- Database schema specification
- Platform inventory (frozen, brief-exact)
- Build plan and mutation log

**Architecture**:
```
docs/
├── adr/
│   ├── 0001-tech-stack.md          # Python/FastAPI/Postgres/Playwright decision
│   ├── 0002-deduplication.md       # Blocking keys, similarity, precision-first policy
│   ├── 0003-scheduling-and-queue.md # Postgres SKIP LOCKED, tiered refresh, circuit breaker
│   ├── 0004-search.md              # Postgres FTS + pg_trgm, ranking, cursor pagination
│   └── 0005-tenants-and-secrets.md # Platform coverage (A), tenant discovery (B), 500k benchmark (C)
├── brief-platforms.txt              # Frozen manifest: 58 ATS + 20 BOARD = 78
├── platforms.md                     # v1.0 — 78 rows with class, wave, transport, coverage
├── schema.md                        # v2 — Complete DB schema with invariants
└── README.md                        # This file
```

**Key Documents**:

### `brief-platforms.txt` (Frozen Manifest)
- **Authority**: Single source of truth for platform names
- **Format**: `<family>\t<exact platform name>` (family: ATS | BOARD)
- **Totals**: ATS = 58, BOARD = 20, total = 78
- **Immutable**: Changes require `plans/MUTATIONS.md` entry

### `platforms.md` (v1.0 — Brief-Exact Inventory)
- 78 rows matching `brief-platforms.txt` row-for-row (both directions)
- Columns: Platform, Family, Class (C1–C4), Wave (S3/S7/S8/S10/S11/S12/S13), Transport, Auth, Risk, Coverage
- **Coverage states**: `outstanding` (Phase 0), `implemented`, `tested`, `verified`
- **Wave ownership**: S3=1, S7=10, S8=6, S10=8, S11=7, S12=20, S13=26 (sums to 78)
- **Class distribution**: C1=11, C2=21, C3=26, C4=20
- **No EX class** — All 78 required (ADR 0005 §A)
- **Coverage is generated** — `jobindex.cli coverage` (S2+) regenerates from DB evidence

### `schema.md` (v2 — Complete DB Schema)
- 7 tables, 14 enums, all constraints, indexes, triggers, views
- Invariants encoded as CHECK constraints and deferred triggers
- Closure state machine: `unverified → open → stale → closed`
- **Invariant**: Failed/incomplete fetch never closes a job
- **Liveness**: Deferred triggers on `job_occurrence.is_live` and `job.status`
- **Canonical consistency**: `job_cluster.canonical_job_id` is authority
- **S1 baseline**: Explicit table set (§13) — no queue/dedup tables

### ADRs (Architecture Decision Records)
| ADR | Title | Status | Key Decision |
|-----|-------|--------|--------------|
| 0001 | Tech Stack | Accepted | Python/FastAPI/Postgres/Playwright |
| 0002 | Deduplication | Proposed | Blocking keys, precision-first, Wilson 95% LB ≥ 0.95 |
| 0003 | Scheduling & Queue | Accepted | Postgres SKIP LOCKED, tiered refresh |
| 0004 | Search | Accepted | Postgres FTS + pg_trgm, cursor pagination |
| 0005 | Tenants & Secrets | Accepted | A=78 platforms (contractual), B=8-10k tenants (feasibility), C=500k (benchmark) |

**Verification**:
- `scripts/verify_phase0.py` — Asserts manifest ↔ inventory row-for-row, class/wave sums, schema refs
- `tests/test_schema_parity.py` — ORM ↔ `docs/schema.md` column parity
- `tests/test_platform_inventory.py` — 78 rows, correct class/wave, all `outstanding`

**Mutation Protocol**:
- All changes to plan, inventory, or schema require `plans/MUTATIONS.md` entry
- Silent reordering or substitution is a process defect
- Platform name changes are scope changes, not edits

**Current Implementation Status**: Phase 0 complete — all documents approved, frozen, and verified.

**Future Documentation Requirements** (planned):
| Step | Document | Description |
|------|----------|-------------|
| S2 | `docs/adding-a-connector.md` | "Add a platform in an afternoon" guide |
| S9 | `docs/dedup.md` | Labeled set, threshold sweep, failure modes |
| S14 | `docs/coverage.md` | Per-platform status, live example, forecast vs measured |
| S14 | `docs/nfr.md` | Actuals vs targets with queries |
| S15 | `docs/runbook.md` | Add/disable source, read failures, rotate credentials |
| S15 | `docs/cost-model.md` | Arithmetic at 500k jobs / 6-24h refresh |
| S15 | `docs/limitations.md` | Top 5 fragility risks + mitigations |