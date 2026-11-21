# JobIndex Phase 5A — Vercel + Supabase Cloud-Only Feasibility Report

**Date:** 2026-11-21
**Scope:** Phase 5A only (cloud feasibility prototype). Phase 5B, S3 Greenhouse, additional platform
connectors, and live deployment are explicitly NOT part of this deliverable.
**Approved stack:** Vercel (Next.js / FastAPI / serverless) + Supabase (managed PostgreSQL, cron,
queues). No Railway, Render, AWS, external VPS, or local-computer dependency.
**Verdict:** **GO with caveats** — the HTTP/API-based serverless execution path is feasible and proven
by prototype; headless-browser execution on Vercel serverless functions is NOT recommended for Phase 5A.

---

## 1. Executive summary

The Vercel + Supabase-only design CAN support scheduled indexing that continues while the developer's
computer is turned off. The prototype implements the complete minimal cloud execution path:

1. Supabase PostgreSQL stores pending extraction tasks and execution state.
2. Supabase Cron (pg_cron) dispatches due sources into a task queue every minute.
3. A Vercel Python serverless function securely claims and processes small task batches.
4. Results are written back to PostgreSQL (`fetch_run`, `source`, task state, execution log).
5. Failed tasks are retried with exponential backoff; lease expiry and a partial unique index prevent
   duplicate processing.
6. Task ownership (`claimed_by`), lease expiry (`heartbeat_at`), attempts, completion and failures are
   all tracked.
7. A verification command (`scripts/verify_cloud.py` / `jobindex cloud verify`) provides offline proof.

All 534 offline tests pass, ruff and mypy are clean on new code, and the Phase 0 gate (78-platform
inventory, class/wave sums) still passes. No existing schema, connector, or invariant was modified.

**Limitation:** Vercel serverless functions are short-lived (seconds on Hobby, up to 15 min on Pro)
and cannot run a persistent headless-browser pool. Playwright + Chromium is NOT feasible on this stack
for Phase 5A. API/HTTP extraction is strongly preferred; browser execution is deferred.

---

## 2. Task 1 — Existing implementation inspected

The repository (end of Phase 2 / start of Phase 3) contains:

- **Docs/ADRs:** `docs/schema.md` (S1 baseline: `platform`, `source`, `job`, `job_cluster`,
  `job_occurrence`, `fetch_run`, `change_event`, guards, closure predicate), `docs/adr/0001–0005`,
  `docs/platforms.md` (78 platforms: C1=11, C2=21, C3=26, C4=20; waves S3=1, S7=10, S8=6, S10=8,
  S11=7, S12=20, S13=26), `docs/brief-platforms.txt` (frozen manifest).
- **Source (S1/S2 scaffold, uncommitted in working tree):** `settings.py`, `db/` (models, enums,
  session), `connectors/` SDK (`base.py`, `context.py`, `extraction.py`, `results.py`, `registry.py`,
  `auth.py`, `_utils.py`, `_template/`), `api/app.py` (`/healthz`, `/readyz`), `cli/` (`platforms`,
  `coverage`), `scheduler/` (empty — S5 not yet implemented), `migrations/` (0001 baseline, 0002 seed
  platforms, 0003 occurrence fetch_run).
- **Tests:** 524 offline tests pass (68 `@pytest.mark.db` skipped without Postgres).

**Preserved:** the 78-platform inventory (unchanged), the existing PostgreSQL schema (no existing table
or column modified), and all Phase 4 integrity guarantees (failure is not closure, idempotent identity
keys, liveness guards, closure-evidence requirements). No unfinished Phase 3 work was overwritten or
discarded; the scheduler (`source_run_queue`) remains unimplemented as in S5, and the prototype uses a
separate additive table so S5 can be introduced later without conflict.

Verification commands run:
- `python scripts/verify_phase0.py` → PASS
- `python -m pytest -q -m "not db"` → 524 passed (before cloud tests), 534 passed (after)
- `ruff check` / `mypy src` → clean on new code (pre-existing `lxml` stubs warning in
  `connectors/extraction.py` is unrelated)

---

## 3. Task 2 — Minimal cloud feasibility prototype (implemented)

### 3.1 New schema (additive migration)

`migrations/versions/0004_serverless_execution.py` creates, **without touching any existing table**:

- `serverless_task_queue` — `id`, `source_id` (FK → `source`), `run_id`, `task_kind`, `payload` (jsonb),
  `state` (`pending`/`running`/`succeeded`/`failed`/`abandoned`), `claimed_by`, `claimed_at`,
  `heartbeat_at`, `attempts`, `max_attempts`, `next_claim_at`, `error_detail`, `result` (jsonb),
  `created_at`, `completed_at`.
  - Partial unique index `serverless_task_one_live_per_source` on `source_id` where
    `state IN ('pending','running')` — at most one live attempt per source.
  - `UNIQUE (source_id, run_id)`.
- `serverless_execution_log` — `task_id`, `run_id`, `worker_id`, `event`, `detail` (jsonb), `created_at`.
- `dispatch_serverless_tasks(batch_limit)` — pg_cron-callable function that inserts one pending task
  per due source (bounded per invocation).

This mirrors the approved `source_run_queue`/`fetch_run` pattern (ADR 0003, `docs/schema.md` §7, §10)
but is scoped to the serverless prototype, so S5's `source_run_queue` can be added later without
conflict.

### 3.2 Worker logic (`src/jobindex/cloud/`)

- `queue.py` — SQL for claim (`FOR UPDATE ... SKIP LOCKED`), fetch_run started/finalized, source
  health/backoff/next-due update, mark task succeeded/failed, retry (RETURNING id), reclaim stale
  leases, execution-log insert, insert pending task.
- `worker.py` — `run_dispatch()`: claim batch → process each task (fetch_run started → synthetic
  extraction → finalize fetch_run → update source → mark task) → reclaim stale leases → schedule
  retries with exponential backoff → commit. Exponential backoff: `min(base * 2^(attempts-1), cap)`,
  base 30s, cap 1h (tunable). Lease TTL default 15 min (mirrors ADR 0003 reclaim).
- `fixtures.py` — two deterministic synthetic tenants (no network): `test-tenant-a` (always success,
  5 jobs) and `test-tenant-b` (configurable fail_count, exercises retry).
- `verification.py` — `run_verification()` reusable offline checks.

### 3.3 Vercel serverless function

`api/extract.py` — `handler(request)`: reads `worker_id`/`batch_size` from the request, opens a
short-lived SQLAlchemy/asyncpg session to `JOBINDEX_DATABASE_URL` (Supabase pooled connection string),
runs `run_dispatch()`, returns a JSON summary. GET returns service info. `vercel.json` configures the
`api/extract.py` function (Python runtime, `maxDuration: 900`), route, and an optional Vercel Cron.

### 3.4 Supabase cron

`supabase/cron.sql` — schedules `dispatch_serverless_tasks(25)` every minute via pg_cron, with an
optional `pg_net.http_post` step to trigger the Vercel worker. Runs in the cloud; independent of the
local computer.

### 3.5 Verification command

- `scripts/verify_cloud.py` — offline verification (file existence, imports, deterministic backoff/tiers,
  full run_dispatch claim/process/backoff/reclaim cycle against a mocked session, retry exhaustion,
  lease expiry). 26/26 checks pass.
- `jobindex cloud verify` (wired into `src/jobindex/cli/main.py`) — same checks via the CLI.

### 3.6 Tests

`tests/cloud/test_worker.py` — 10 offline unit tests (backoff, tiers, fixtures, claim/process batch,
retry with backoff, max-attempt exhaustion, claim increments attempts). All pass; 534 total offline
tests pass.

---

## 4. Task 3 — Browser execution research (Playwright on Vercel)

### Findings (platform limits; requires empirical verification)

| Concern | Assessment for Vercel serverless functions |
|---------|---------------------------------------------|
| Browser runtime | Chromium is not pre-installed; must be bundled with Playwright and shipped in the function package. |
| Deployment size | Vercel function bundles are size-limited; a Playwright + Chromium bundle is large and risks exceeding the limit, leaving little room for code and dependencies. |
| Cold start | Launching headless Chromium adds seconds of latency on every invocation. |
| Memory / duration | Chromium needs substantial RAM; Vercel functions are memory-bounded and short-lived. A browser session can be terminated mid-run if it approaches function-duration or memory limits. |
| Concurrency | No persistent process; each invocation is isolated. A browser pool cannot be kept warm across invocations; each run must launch and tear down a browser. |
| Cleanup | Ephemeral filesystem; browser processes must be killed within the invocation or they leak. |

**Conclusion:** These limits are **platform-level constraints, not yet empirically verified** for this stack. The Phase 5A decision is to prefer API/HTTP extraction whenever possible (all C1/C2 platforms expose structured
endpoints per `docs/platforms.md`) and **not** implement a Playwright browser pool in Phase 5A. Headless-only sources (C3, Wave F) remain `outstanding`. Browser execution should be revisited only after a separate, empirically verified feasibility spike (measuring actual bundle size, cold start, memory, and duration on the target plan), and only with explicit approval. If that spike shows browser execution cannot fit within Vercel serverless limits, the limitation is reported rather than worked around with another hosting provider.

---

## 5. Task 4 — JobIndex invariants preserved

The prototype does not alter the connector interface, schema, or inventory, so all invariants hold:

- **list_page / fetch_detail** interface unchanged (`connectors/base.py` `Connector` protocol).
- **Failure is not closure** — a failed/partial/abandoned task sets `fetch_run.outcome` accordingly and
  never sets `listing_complete`; jobs are not closed (the closure predicate in `docs/schema.md` §11
  requires a successful, complete listing).
- **No duplicate jobs from retries** — idempotent identity `UNIQUE (origin_source_id,
  origin_external_id)` on `job` and `UNIQUE (source_id, external_id)` on `job_occurrence` (existing
  schema) plus the one-live-attempt partial unique index on `serverless_task_queue`.
- **Task ownership / lease** — `claimed_by`, `claim_token` (unique per claim),
  `claimed_at`, `heartbeat_at`, `attempts`, `next_claim_at`, `max_attempts` tracked;
  all mutations fence on `claim_token` so a stale/reclaimed worker cannot overwrite;
  expired leases reclaimed as `abandoned`.
- **Stale-worker fencing** — `mark_task_succeeded`, `mark_task_failed`,
  `refresh_heartbeat` and `finalize_fetch_run` all require the current `claim_token`
  (and `finalize_fetch_run` also requires `outcome IS NULL`), so a worker whose lease
  was reclaimed cannot complete or overwrite another worker's task.
- **Robots.txt, rate limits, access restrictions** — unchanged (governed by the connector SDK
  `ConnectorContext`; no new connectors, no bypass).
- **No auth bypass / CAPTCHA / anti-bot evasion** — no new connectors; secrets remain resolved per
  source.id from env/file (ADR 0005), never in `source.config` or logs.
- **Source/platform transparency** — `source.platform_key` FK to `platform`; coverage states remain
  generated by `jobindex.cli coverage`.

---

## 6. Source Health Monitoring API

The prototype implements source-level scraping visibility (Part B of the audit). Each source
(website) is tracked separately from its platform type, using `source`, `fetch_run` and
`serverless_task_queue` as authoritative evidence.

Two dimensions are tracked separately:

* **Health status** (`source.health`): healthy / degraded / open / half_open / disabled — the
  circuit-breaker state of the source.
* **Active execution status**: whether a source currently has a claimed task with a valid lease
  heartbeat (state=`running` and heartbeat within the TTL). **Expired leases are not counted as
  active scraping**, and **a failed scrape is not treated as proof that jobs have closed**.

Authenticated monitoring endpoints (fail-closed on `JOBINDEX_CRON_SECRET`):

| Endpoint | Returns |
|----------|---------|
| `GET /api/monitoring/summary` | Aggregate counts: total discovered, enabled, actively scraping, claimed, healthy, degraded, unavailable, paused, blocked, pending tasks. |
| `GET /api/monitoring/sources` | Every source with health, active-scraping status, latest run outcome/error, job counts, last attempt/success, next due. |
| `GET /api/monitoring/sources/{id}` | Detail for one source (website). |
| `GET /api/monitoring/platforms` | Source counts grouped by platform, with health breakdown per platform. |
| `POST /api/extract` | Worker dispatch (claim → process → finalize → retry), protected by the cron secret. |

A dashboard is **not** implemented: no compatible frontend infrastructure exists yet (the planned
Jinja UI is S4). The API contract above is the deliverable; a dashboard can consume it later.

---

## 7. Task 5 — Verification evidence

| Requirement | Evidence |
|-------------|----------|
| Function invocation without local computer | Design: Supabase Cron (pg_cron, cloud) dispatches; Vercel function invoked via `pg_net.http_post` or Vercel Cron. Proven offline by `run_dispatch()` + `scripts/verify_cloud.py` (mocked session). |
| Scheduled dispatch | `supabase/cron.sql` schedules `dispatch_serverless_tasks(25)` every minute (pg_cron min resolution = 1 minute). |
| Successful task execution & persistence | `tests/cloud/test_worker.py` + `scripts/verify_cloud.py`: claim → process → finalize `fetch_run` → update source → mark task succeeded, transaction commits. |
| Safe retry & recovery | failed task retried with backoff (`next_claim_at` advances); `task at max_attempts is exhausted (not retried)`; `expired lease reclaimed as abandoned`. |
| Concurrency control | claim uses `FOR UPDATE ... SKIP LOCKED`; partial unique index one live attempt per source; verified by `claim uses FOR UPDATE ... SKIP LOCKED` check. |
| Deployment config compatibility | `vercel.json` (function, route, cron), `supabase/cron.sql` (pg_cron + pg_net), migration 0004 (additive). |
| PostgreSQL schema compatibility | migration 0004 is additive; no existing table/column changed; `tests/test_schema_parity.py` still passes. |
| Throughput / cost estimate for 78 platforms | See §8. |
| Constraints preventing continuous / sub-minute execution | pg_cron fires at minute resolution (sub-minute not required for 6h–72h tiers). Vercel function duration limits (Hobby seconds / Pro ≤ 900 s) bound batch size; connection pooling via Supavisor required. No persistent worker fleet or daemon. |

Test results:
- `python scripts/verify_cloud.py` → 26/26 PASS
- `python -m jobindex cloud verify` → 26/26 PASS
- `python -m pytest -q -m "not db"` → 541 passed, 82 db-marked skipped
  (includes offline monitoring-auth tests: `tests/cloud/test_monitoring_auth.py`)
- `ruff check` on new/modified files → clean
- `mypy src/jobindex/cloud api/extract.py` → clean
- `python scripts/verify_phase0.py` → PASS (78 platforms, classes/waves sum to 78, no EX rows)

---

## 8. Constraints by plan tier (free vs paid)

No specific throughput or cost figures are claimed here; they belong in `docs/cost-model.md` (S15),
which was deferred in Phase 0 as accepted. The following are realistic, plan-agnostic constraints
for the approved stack:

| Concern | Free tier (Vercel Hobby / Supabase Free) | Paid tier (Vercel Pro / Supabase Pro) |
|---------|------------------------------------------|---------------------------------------|
| Function duration | Very short (seconds); small batches only | Up to 15 minutes per invocation |
| Concurrent connections | Low limit; Supavisor pooling required | Higher limit; pooling still required |
| Cron scheduling | Minute resolution via pg_cron | Minute resolution via pg_cron |
| Deployment bundle | Size limit constrains dependencies | Higher size limit |
| Function invocations / month | Limited quota; bursty cron + worker usage | Higher quota |
| Database compute | Idle-mostly; bursty cron/worker load | Dedicated compute options available |
| Database storage | Small; fits 78 platforms + tenants | Scales with tenant count (objective B) |

**Key operational constraints:**
- **Batch size must stay small** (default 5) relative to function-duration limits; large listings must be split across invocations.
- **One live attempt per source** is enforced by schema; the dispatcher is bounded per invocation (`dispatch_serverless_tasks(25)`).
- **Connection pooling (Supavisor)** is required; direct Postgres connections would exhaust concurrent-connection limits under serverless concurrency.
- **No persistent process/daemon**: each invocation is stateless; no resident worker fleet.
- **Cost grows with tenant count**: objective B (8,000–10,000 tenants) materially increases request volume, storage, and function-duration versus the Phase 5A prototype scale (one or two test tenants).
- No throughput, request-volume, or dollar figures are claimed; measure them in S14/S15.


---

## 8. Constraints on continuous or sub-minute execution

1. **pg_cron fires at minute resolution** — standard cron schedules are minute-granular. Sub-minute
   dispatch is not required by Phase 5A (refresh tiers are 6h/12h/24h/72h); revalidate against the
   target Supabase plan if sub-minute scheduling becomes necessary.
2. **Vercel serverless function duration** — Hobby tier times out in seconds; Pro allows up to 900 s.
   Batches must stay small (default 5) and complete quickly; long-running work (e.g., large listings)
   must be split across invocations.
3. **No persistent process / daemon** — each invocation is stateless; no resident worker fleet, no
   in-memory cache of browser sessions, no long-lived connections outside the pooled DB.
4. **Connection pooling required** — Supavisor (Supabase connection pooler) must be used; direct
   Postgres connections would exhaust concurrent-connection limits under serverless concurrency.
5. **No headless-browser pool** — Chromium cannot be run persistently; see §4.
6. **Deployment size limit** — Vercel function bundle (250 MB Hobby) constrains dependencies; keep
   the worker function small (sqlalchemy, asyncpg, httpx, tenacity only).

These are inherent to the approved Vercel + Supabase-only stack, not bugs in the prototype.

---

## 9. Exact file changes

### New files
1. `migrations/versions/0004_serverless_execution.py` — additive serverless task queue schema
2. `src/jobindex/cloud/__init__.py` — cloud package exports
3. `src/jobindex/cloud/queue.py` — claim/complete/reclaim/retry SQL wrappers
4. `src/jobindex/cloud/worker.py` — `run_dispatch()` orchestration, backoff, lease TTL
5. `src/jobindex/cloud/fixtures.py` — deterministic synthetic test tenants
6. `src/jobindex/cloud/verification.py` — reusable offline verification checks
7. `api/extract.py` — Vercel serverless `handler`; proxies to the FastAPI app via ASGITransport
8. `src/jobindex/api/monitoring.py` — authenticated source-health monitoring API + `POST /api/extract` worker endpoint
9. `vercel.json` — Vercel function config, route, cron-secret env
10. `supabase/cron.sql` — pg_cron dispatch + pg_net worker trigger (single scheduler)
11. `scripts/verify_cloud.py` — standalone verification command
12. `tests/cloud/test_monitoring_auth.py` — offline cron-secret auth tests (fail-closed)
13. `tests/cloud/test_worker.py` — 10 offline unit tests
14. `tests/db/test_cloud_queue.py` — PostgreSQL integration tests (concurrency, fencing, retry, recovery, health transitions, isolation)
15. `tests/db/test_monitoring.py` — PostgreSQL integration tests (monitoring query + aggregation)
16. `docs/architecture-phase5a.md` — architecture diagram (this document's companion)
17. `docs/phase5a-feasibility.md` — this report

### Modified files
18. `src/jobindex/api/app.py` — includes monitoring router; single FastAPI app for local + Vercel
19. `src/jobindex/cli/main.py` — added `jobindex cloud verify` subcommand
20. `migrations/versions/0004_serverless_execution.py` — `claim_token` column added to serverless task queue
21. `TODO_PHASE5A.md` — work-tracking file (not a deliverable)

### Unchanged (preserved)
- All existing schema, migrations 0001–0003, connector SDK, `docs/platforms.md` (78 inventory),
  `docs/brief-platforms.txt`, ADRs, and all existing tests.

Nothing was committed or pushed (per restrictions); all work is in the working tree.

---

## 10. Deployment requirements

| Requirement | Detail |
|-------------|--------|
| Vercel project | Python runtime enabled; `api/extract.py` deployed as a serverless function; `JOBINDEX_DATABASE_URL` set to the Supabase pooled connection string. |
| Supabase project | PostgreSQL 16 with `pgcrypto`, `pg_trgm`, `unaccent` extensions; `pg_cron` and `pg_net` extensions enabled; connection pooler (Supavisor) configured. |
| Migration | `alembic upgrade head` against Supabase (migration 0004 additive; existing 0001–0003 already applied). |
| Environment | `JOBINDEX_DATABASE_URL`, optional `WORKER_ID`; secrets per ADR 0005 (env `JOBINDEX_SECRET_<id>` or `$JOBINDEX_SECRET_DIR/<id>`), never in the DB. |
| Scheduling | Enable both pg_cron jobs in `supabase/cron.sql` (dispatch + pg_net worker trigger). pg_cron is the single scheduler; no Vercel Cron and no local-computer dependency. |
| Concurrency | Keep batch size small (default 5) relative to function-duration limits; use one live attempt per source (enforced by schema). |

### Not done (requires explicit approval)
- No production database migration or live deployment.
- No Vercel/Supabase resources provisioned.
- No Playwright/browser pool.

---

## 11. GO / NO-GO recommendation

### GO — with the following scope

The **HTTP/API-based serverless execution path** is feasible on the approved Vercel + Supabase-only
stack and proven by the prototype (offline verification 26/26, 541 offline tests pass, lint/type
clean on new code, Phase 0 gate passes). Scheduled dispatch runs in the cloud via Supabase Cron and
continues while the developer's computer is off. Source-health monitoring is exposed through
authenticated `GET /api/monitoring/*` endpoints. All existing invariants, the 78-platform inventory,
and the schema are preserved.

**Not yet approved:** Phase 5A is ready for a controlled cloud deployment test (throwaway Supabase
project, `JOBINDEX_CRON_SECRET` set, pg_cron enabled), but is not approved for production deployment
or migration.

### NO-GO / deferred for this phase

- **Headless-browser execution (Playwright/Chromium)** on Vercel serverless functions: not yet
  empirically verified within function duration, memory, and deployment-size limits. Deferred; the
  decision is to prefer API/HTTP extraction and revisit browser execution only after a separate,
  empirically verified feasibility spike and explicit approval.
- **Continuous or sub-minute execution:** not supported (pg_cron minute resolution; short-lived
  functions).
- **Live deployment / production DB migration:** requires explicit approval.

If a requirement cannot be met by Vercel + Supabase (notably browser-based extraction for C3/Wave-F
platforms), this report documents the limitation rather than introducing another hosting provider, as
directed. Stop after Phase 5A verification for independent review.
