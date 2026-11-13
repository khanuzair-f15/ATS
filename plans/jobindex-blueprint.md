# JobIndex — Construction Plan

**Objective:** A single, deduplicated, searchable index of job postings harvested from 78
source platforms (58 ATS + 20 job boards), with auditable freshness, correct
closure-vs-failure semantics, and a handoff package a new team can operate.

**Scope class:** EPIC. Multi-session, multi-wave. Not a single-pass build.

**Status:** Phase 0 complete on approval of this plan. No connector code until then.

---

## 0. Preconditions (verified during planning)

| Check | Result |
|---|---|
| Working tree | `D:\ATS`, empty at plan time |
| Git | 2.55.0 present |
| GitHub CLI | **absent** → direct mode, no automated PR/CI |
| Python | 3.12.10 present; `py` launcher broken, use `python` |
| Postgres | not yet provisioned (Step 1) |
| Stakeholder decisions | Python/FastAPI; acquisition-demo bar; ~500k active jobs at 6–24h refresh; structured sources first, headless as last resort |

**Workflow for every step (direct mode):** create a local branch `step/<n>-<slug>`,
do the work, run the step's verification command, merge to `main` with a
conventional commit. No remote, no PR. The commit is the review artifact.

---

## 1. Invariants (hold at every step; a step that breaks one is reverted)

1. **Failure is not closure.** No timeout, 429, 5xx, TLS error, or parse error may
   move a job to `closed`. Only positive evidence may (404/410 on detail URL,
   explicit closed marker in payload, or absence from a *successful full* listing
   fetch).
2. **A job with zero live occurrences cannot be `open`.**
3. **Every failed fetch leaves a `fetch_run` row.** No silent failures.
4. **`first_seen_at` is immutable.** `last_checked_at` advances only on a completed fetch.
5. **Every job retains every source URL and every apply URL.** Dedup merges
   canonical fields, never occurrence rows.
6. **Nothing is claimed working without a live example and a `fetch_run` row.**
7. **CI never touches the network.** All connector tests replay recorded fixtures.
8. **No auth bypass, no paywall circumvention, no logged-in scraping.**

---

## 2. Dependency graph

```
                         S0 → S1 → S2 → S3 → S4 → S5 → S6
                                                        │
                    ┌───────────────────┬───────────────┴───────────────┐
                    ▼                   ▼                               ▼
                   S7                  S10                             S12
                    │                   │                               │
                    ▼                   ▼                               │
                   S8                  S11                              │
                    │                   │                               │
                    └──────────┬────────┴───────────────┬───────────────┘
                               ▼                        │
                              S13                       │
                               │                        │
                               │      S7 ──┬──► S9 ◄───┘   (S9 needs S7 + S12)
                               │           │
                               └─────┬─────┴──────────────► S14
                                     │                       │
                                     ▼                       ▼
                                    S15
```

Parallel groups: `{S7, S10, S12}` after S6. `S8` waits on `S7`; `S11` waits on
`S10`; `S9` waits on `S7` **and** `S12` (dedup needs cross-source overlap, and the
only reliable overlap comes from boards re-posting ATS jobs); `S13` waits on all four
connector waves so it captures only genuine residuals; `S14` waits on `S9` **and**
`S13` so the coverage report cannot be generated before the last connector wave
finishes.

---

## 3. Steps

### S0 — Phase 0 decision set (this session)
**Parallel group:** none · **Model:** strongest · **Status:** done on approval

**Context brief:** Greenfield. Nothing exists. The three highest-leverage decisions
(stack, schema shape, platform classification) are cheapest to change now and
most expensive to change after 20 connectors. This step produces documents only.

**Tasks**
- `docs/adr/0001-tech-stack.md` — 3 options compared, decision, reversal cost.
- `docs/adr/0002-deduplication.md` — blocking keys, similarity, hard constraints,
  precision-first policy, eval protocol.
- `docs/adr/0003-scheduling-and-queue.md` — Postgres `SKIP LOCKED`, at-least-once,
  tiered refresh, circuit breaker.
- `docs/adr/0004-search.md` — Postgres FTS + `pg_trgm`, ranking, cursor pagination,
  measured triggers for adding an external engine.
- `docs/adr/0005-tenants-and-secrets.md` — how ~500k jobs is reached from 78 platforms
  (tenant acquisition arithmetic), tenant lifecycle, and secret resolution.
- `docs/schema.md` — `platform`, `source`, `job`, `job_cluster`, `job_occurrence`,
  `fetch_run`, `change_event`, enums, indexes, invariants, closure state machine,
  liveness enforcement, explicit S1 table set.
- `docs/platforms.md` — all 78 classified by transport, **owned by exactly one wave**,
  verification status, expected request volume, risk, and what is explicitly out of scope.
- `plans/MUTATIONS.md` — the plan mutation log, opened with M-001 (review remediation).
- This plan.

**Verification:** `docs/platforms.md` row count = 78; every platform row has exactly one
`Wave`, the wave counts sum to 78, and the class counts match the stated distribution;
every table in `docs/schema.md` has a stated primary key and owner; ADR 0001 states a
decision, not a survey.

**Exit criteria:** stakeholder approves wave sequencing in §2.

**Rollback:** delete `docs/` and `plans/`. Nothing depends on them yet.

---

### S1 — Core scaffold, fixture harness, first green test
**Deps:** S0 · **Model:** default

**Context brief:** Before any connector, the repo needs a shape that survives 78 of
them. The single most important artifact here is the **offline test harness** — if
connectors are tested against the live internet, every wave becomes flaky and
unreviewable. `pytest` must be able to run the full suite with networking disabled.

**Tasks**
- `pyproject.toml` (uv or pip, Python 3.12), `src/jobindex/` layout:
  `connectors/`, `normalize/`, `db/`, `api/`, `scheduler/`, `dedup/`, `web/`.
- `docker-compose.yml`: Postgres 16 with `pg_trgm`, `unaccent`, `pgcrypto`.
- SQLAlchemy 2.x async engine + Alembic baseline. **The baseline migration creates
  exactly this set, no more:** enums `transport_class`, `platform_class`,
  `refresh_tier`, `source_health`, `tenant_status`, `job_status`, `remote_status`,
  `employment_type`, `salary_period`, `fetch_outcome`, `error_class`,
  `verification_status`, `closure_evidence`; tables `platform`, `source`, `job`,
  `job_cluster`, `job_occurrence`, `fetch_run`, `change_event`; every constraint,
  generated column and index in `docs/schema.md` §2–§11 except the queue and dedup
  tables. `platform` is seeded from `docs/platforms.md` in a data migration, and a
  test asserts the table and the document agree row-for-row.
- `settings.py` via `pydantic-settings`; `.env.example`; secrets never committed.
- `GET /healthz` and `GET /readyz` (readiness checks DB round-trip).
- `pytest` + `pytest-asyncio`; a `no_network` autouse fixture that fails any test
  attempting a real socket.
- `Makefile` (or `justfile`) targets: `up`, `migrate`, `test`, `lint`, `fmt`.

**Verification**
```bash
docker compose up -d && python -m alembic upgrade head
python -m pytest -q          # green, 0 network calls
curl -s localhost:8000/readyz
```
**Exit criteria:** clean checkout → `up`, `migrate`, `test` all succeed; `/readyz` 200.
**Rollback:** scaffold is additive; revert the branch.

---

### S2 — Connector interface, SDK, and fixture capture tool
**Deps:** S1 · **Model:** strongest

**Context brief:** 78 connectors only stay maintainable if writing one is mechanical.
The interface must be transport-agnostic so the scheduler cannot tell HTTP+JSON from
DOM parsing from a headless browser. The capture tool is what makes live debugging
produce permanent offline tests.

**Tasks**
- `connectors/base.py`: `Protocol` with `detect`, `list_jobs`, `fetch_job`,
  `classify_error`; shared `ConnectorContext` (httpx client, rate limiter, UA,
  robots gate, logger).
- `ConnectorResult` types; `NormalizedJob` / `RawOccurrence` dataclasses mirroring
  `job` / `job_occurrence`.
- Error taxonomy enum: `transient | permanent | rate_limited | blocked | not_found`.
- `tools/capture.py`: given a URL, record status, headers, body into
  `tests/fixtures/<connector>/<case>/` with a manifest (sha256, captured_at, UA).
- `tools/replay.py`: fixture-backed transport that drops into `ConnectorContext`.
- `connectors/_template/` — a runnable reference connector plus
  `docs/adding-a-connector.md` (the "add a platform in an afternoon" guide).
- `jobindex.cli coverage` — **ships here, not in S14.** It reads `platform`,
  `source`, `fetch_run` and regenerates `docs/platforms.md`'s `Verified` column and
  `docs/coverage.md`. Generating evidence must exist before the first connector, or
  `verified` is a human-edited claim (review C9). S14 extends this command with NFR
  measurements; it does not introduce it.
- Rate limiter: default 1 concurrent request per host, jittered delay, configurable
  per connector; identifiable UA with contact URL; `robots.txt` enforced for
  HTML-class connectors only.

**Verification:** `python -m pytest tests/connectors/test_template.py -q`; template
connector produces normalized output from its own fixture with networking blocked.
**Exit criteria:** a new connector can be added by copying `_template/` and nothing
else; `jobindex.cli coverage` reproduces `docs/platforms.md` byte-for-byte from the DB
with every row still `no`.
**Rollback:** revert branch; no data written.

---

### S3 — Slice 1: Greenhouse end-to-end (list → detail → normalize → upsert)
**Deps:** S2 · **Model:** default

**Context brief:** This is the shape-validation step. If the pipeline is wrong, it is
wrong 78 times. One platform, one real tenant, all the way into Postgres, including
`sources`, `jobs`, `job_occurrences`, and a `fetch_runs` row.

**Tasks**
- `connectors/greenhouse.py`: board token discovery from an entry URL; list endpoint
  with content included; per-job detail; `external_id` = posting id.
- Normalizer: HTML → sanitized markdown; location parsing (`city/region/country/raw`);
  remote-status inference with `unknown` as the honest default; employment-type enum
  mapping; salary parsing with raw string always preserved.
- Upsert `job` on `(origin_source_id, origin_external_id)` and `job_occurrence` on
  `(source_id, external_id)`; content hash for change detection; `change_event` rows
  on field drift.
- Recorded fixtures for ≥2 distinct employers (proves the connector is not
  hardcoded to one tenant).

**Verification**
```bash
python -m pytest tests/connectors/test_greenhouse.py -q   # offline, goldens
python -m jobindex.cli fetch --source <id>                # live, manual
psql -c "select count(*) from job_occurrence"             # > 0
psql -c "select outcome, jobs_found from fetch_run"
```
**Exit criteria:** ≥2 employers ingested; every record has title, company,
description, location, source_url, apply_url; re-running is idempotent (no dupes).
**Rollback:** set `source.enabled = false` and leave the rows in place. Occurrences are
never deleted (schema §14), and `job_occurrence` has no `ON DELETE CASCADE`, so
"delete the source row" is **not** a valid rollback. To discard the data entirely,
drop and re-create a **development** database; never delete source rows in a database
whose occurrences must survive.

---

### S4 — Search API + minimal UI on one source
**Deps:** S3 · **Model:** default

**Context brief:** Prove the consumer shape now, while only one source exists and
schema changes are cheap. Postgres FTS + trigram, versioned `/v1`, cursor pagination
with stable ordering. UI is deliberately minimal: query box, filters, results, apply
link, "also seen on".

**Tasks**
- OpenAPI-first: `docs/openapi.yaml` committed before handlers.
- `GET /v1/jobs` (keyword, company, location, remote, employment_type, salary_min,
  posted_after, status; opaque cursor; stable `(sort_at DESC, id DESC)` ordering,
  where `sort_at = coalesce(posted_at, first_seen_at)` is `NOT NULL` — see ADR 0004).
- `GET /v1/jobs/{id}` — canonical record + all occurrences with source/apply URLs.
- `GET /v1/sources`, `GET /v1/coverage`.
- GIN index on the generated `search_tsv`, `pg_trgm` GIN on company/location;
  `ts_rank_cd` ordering. The column definition lives in `docs/schema.md` only.
- Server-rendered minimal UI (Jinja) — no SPA build step to hand off.

**Verification:** `pytest tests/api -q`; `curl '/v1/jobs?q=engineer&remote=remote'`;
UI loads and every result exposes a working apply link.
**Exit criteria:** p95 < 300ms at **500k** synthetic rows (seeded benchmark test), and
pagination returns no duplicate or skipped row while rows are inserted mid-page.
**Rollback:** API is additive over existing tables.

---

### S5 — Scheduler, worker lifecycle, tiered freshness, circuit breaker
**Deps:** S4 · **Model:** strongest

**Context brief:** The scheduler is where "current" is either true or a lie. Durable
queue, at-least-once delivery, idempotent writes, per-source isolation, and
degradation instead of mass closure.

**Tasks**
- Postgres queue via `FOR UPDATE SKIP LOCKED`; jobs keyed `(source_id, run_id)`;
  idempotent claim/heartbeat/complete; stale-claim reclaim.
- Refresh tiers on `source` (`hot` 6h, `warm` 12h, `cool` 24h, `dormant` 72h),
  tunable by config row, not redeploy.
- Per-source circuit breaker: N consecutive failures → exponential backoff with
  jitter → `degraded` health state + alert event.
- Worker pool: per-host concurrency 1, global concurrency cap, per-run timeout,
  graceful shutdown that completes in-flight sources.
- Every terminal path writes `fetch_run` with outcome, HTTP status, counts, error class.
- Per-source metrics: success rate, p50/p95 latency, found/new/updated/closed,
  staleness age.

**Verification:** chaos test — one source forced to 500 repeatedly while 5 healthy
sources run; healthy sources unaffected, failing source backs off, zero closures.
**Exit criteria:** full-fleet refresh completes inside its window without overlap.
**Rollback:** disable scheduler container; data remains queryable.

---

### S6 — Closure-vs-failure invariant suite
**Deps:** S5 · **Model:** strongest

**Context brief:** This is the requirement most job indexes get wrong and the one an
acquirer will test. It gets its own step and its own executable proof.

**Tasks**
- State machine: `open → stale → closed` with allowed transitions and required
  evidence; `closed` requires an `evidence_kind` value.
- Tests: 404 closes; 410 closes; explicit payload marker closes; absence from a
  successful full listing marks `stale` then closes after policy window; 429 does
  *not* close; timeout does *not* close; 5xx does *not* close; partial listing does
  *not* close; parse failure does *not* close.
- `partial` outcome definition: listing page N of M failed → never eligible for closure.
- Reopen path: a previously closed occurrence reappearing returns the job to `open`.

**Verification:** `pytest tests/invariants -q` — table-driven, one test per row above.
**Exit criteria:** every listed behaviour has a passing named test.
**Rollback:** n/a (adds tests + narrowing logic).

---

### S7 — Wave A: class-1 ATS residual (10 platforms)
**Deps:** S6 · **Parallel group:** A · **Model:** default

**Context brief:** Pattern-following work now that the SDK exists. Each connector:
detect, list, detail, error classification, ≥2 tenant fixtures, golden output.

**Platforms:** Lever, Ashby, SmartRecruiters, Workable, Recruitee, Breezy HR, Pinpoint,
Comeet, Softgarden, Sympa. (Greenhouse was S3; these are the remaining C1 rows.)

**Per-connector definition of done**
1. `connectors/<platform>.py` implements the Protocol.
2. Fixtures for ≥2 distinct employers, captured live, replayed offline.
3. Golden normalized output reviewed by hand for at least 3 records.
4. A `fetch_run` row from a real run exists for the connector, and
   `python -m jobindex.cli coverage` regenerates the `Verified` column. The table is
   **never** hand-edited to say `yes`.
5. A live example URL and sample job id are present in the generated `docs/coverage.md`.

**Verification:** `pytest tests/connectors -q`; `python -m jobindex.cli coverage --wave A`.
**Exit criteria:** 10/10 working on ≥2 tenants; zero network in CI; coverage table
regenerated and committed as a diff.
**Rollback:** per-connector revert; disable source flag.

---

### S8 — Wave B: mid-market embedded-JSON / XHR (15 platforms)
**Deps:** S7 · **Parallel group:** B · **Model:** default

**Platforms:** Teamtailor, Personio, BambooHR, HiBob, Rippling, Zoho Recruit, Newton,
Gupy, Factorial, Kenoby, Deel, BrassRing, SilkRoad, Talentsoft, eRecruiter.

Same definition of done as S7. These are tenant-templated but structurally simpler than
the enterprise suites, which is why they follow Wave A before Wave C/D. Any platform
that turns out not to expose a structured board is **demoted to class 3 and deferred to
S13**, not hacked inline.

---

### S9 — Deduplication engine + labeled evaluation
**Deps:** S7, S12 (needs ATS **and** board sources to overlap) · **Parallel group:** B
· **Model:** strongest

**Context brief:** The core intellectual deliverable. Precision over recall: a merged
pair of distinct reqs is worse than an unmerged duplicate. Blocking keys keep it
sub-quadratic; thresholds come from measurement, not taste.

**Why S12 and not S7:** a dedup engine evaluated only on ten ATS connectors has almost
no true cross-source overlap — each ATS board is its own tenant universe. The board
wave is where the same req visibly appears twice, so it is the first honest evaluation
set.

**Tasks**
- Blocking: `(company_domain, title_stem)`, `(company_normalized, city_key)`,
  `(apply_host, external_id)` across sources, `(company_domain, apply_host)`.
  Every component used is a real column in `docs/schema.md` §5.
- Similarity: normalized title (token set ratio), company, location, description
  shingles (MinHash or trigram Jaccard). Embeddings only if they beat cheap methods
  on the eval set.
- Hard constraints: differing apply host **and** differing external id **and**
  differing city ⇒ no merge without high description similarity (ADR 0002 §4).
- Cluster assignment: size-capped union-find over accepted pairs; `job.cluster_id` +
  `is_canonical` + `merged_into_job_id`; per-occurrence titles/URLs preserved.
- `docs/dedup.md`: labeled set, threshold sweep, named failure modes.

**Evaluation protocol (blocking gate)**
- ≥400 labeled **true-duplicate** pairs and ≥400 labeled **hard negatives**, drawn from
  real captured Wave-E data. Not invented, not padded with easy negatives.
- **30% holdout**, never touched during threshold tuning; the reported number comes
  from the holdout only.
- Gate: **Wilson 95% lower bound on precision ≥ 0.95** on the holdout. A point estimate
  of 0.95 on 50 pairs is not evidence; the lower bound is.
- Recall is reported honestly with its own interval. It is not gated.

**Verification:** `python -m pytest tests/dedup -q && python -m jobindex.cli dedup eval`.
**Exit criteria:** holdout precision lower bound ≥ 0.95; the exact command and its
output are pasted into `docs/dedup.md`; if the gate fails, thresholds do not ship and
ADR 0002 stays `Proposed`.
**Rollback:** `cluster_id`/`is_canonical` are nullable/defaulted; the engine is a batch
job that can be re-run from scratch with a new `dedup_version`.

---

### S10 — Wave C: enterprise embedded-JSON, batch 1 (8)
**Deps:** S6 · **Parallel group:** A · **Model:** default

**Context brief:** Distinct transport problem: JS-heavy career sites backed by a
JSON/XML endpoint. Higher request cost, tenant path differences, sturdier payloads.

**Platforms:** Workday, SAP SuccessFactors, Oracle Taleo (TBE), Oracle Taleo (TEE),
Oracle Recruiting Cloud, iCIMS, Phenom People, Eightfold AI.

`Eightfold` has **no endpoint claim** in `docs/platforms.md` and is probe-first: if the
probe finds nothing structured, it is demoted to S13 rather than guessed at.

**Extra requirements:** tenant/site path templating; pagination contract tests;
per-tenant config table so a new employer is configuration, not code.

**Exit criteria:** each working on ≥2 tenants (≥1 for Taleo variants if tenant supply
is thin — recorded as a known limitation, not hidden).

---

### S11 — Wave D: enterprise embedded-JSON, batch 2 (8)
**Deps:** S10 · **Parallel group:** B · **Model:** default

**Platforms:** Cornerstone OnDemand, Dayforce, UKG Pro, ADP Workforce Now, Paylocity,
Paycom, Bullhorn, PeopleSoft eRecruit.

Reuses the tenant-config machinery from S10. Same evidence bar. These are the highest
`Risk: H` C2 rows, which is why they come last among the structured waves.

---

### S12 — Wave E: job boards (structured subset)
**Deps:** S6 · **Parallel group:** A · **Model:** default

**Context brief:** Boards are the legal and fragility frontier. Several major boards
prohibit automated collection. Those are documented as **excluded** with reasoning,
not half-implemented. This wave is also S9's evaluation set, so it is not optional
breadth.

**In scope (9, all C4):** Remote OK, Remotive, We Work Remotely, Himalayas, Jobicy,
Working Nomads, Hacker News "Who is hiring", WP Job Manager, Niceboard.

**Deferred to S13 (2, C3):** Built In (HTML + `JobPosting` JSON-LD), Otta / Welcome to
the Jungle (HTML listing).

**Excluded (9, documented):** LinkedIn, Indeed, Glassdoor, ZipRecruiter, Monster, Dice,
SimplyHired, Wellfound, YC Work at a Startup — ToS prohibitions, paid/partner-only APIs,
or session gating. `docs/platforms.md` shows the reason per row and the coverage math
both ways.

---

### S13 — Wave F: HTML / headless residual (18)
**Deps:** S7, S8, S10, S11, S12 · **Parallel group:** B · **Model:** default

**Context brief:** Only sources with no structured alternative justify a browser.
Each one needs a written justification in `docs/platforms.md` and a fragility note.

**In scope (18):** Avature, PageUp, Paycor, JazzHR, Jobvite, Trakstar Hire, JobScore,
PCRecruiter, CATS, Freshteam, Manatal, ClearCompany, ApplicantPro, Hireology,
TalentReef, Harri, Built In, Otta / Welcome to the Jungle — **plus** any C2 row demoted
during S7/S8/S10/S11 probing.

**Tasks:** Playwright-based connector base (browser pool, reuse, block-list of
trackers, hard timeouts, screenshot on failure into the run record); convert each
residual only if `docs/platforms.md` shows no class-1/2 path. Everything else stays
unverified and is listed as such.

---

### S14 — Coverage report + NFR measurement (final wave accounting)
**Deps:** S9, S13 · **Model:** default

**Context brief:** The coverage command shipped in S2; this step is where it is
**finished and trusted** — the last connector wave has run, so the report is now a
measurement rather than a forecast.

**Tasks**
- Extend `python -m jobindex.cli coverage` to emit `docs/coverage.md` and JSON:
  per platform status, live example, sample job, last success, connector path,
  tenants proven, open failures, and **forecast vs measured** against
  `docs/platforms.md` §3.
- Regenerate the `Verified` column of `docs/platforms.md`; the working tree must be
  clean after regeneration (proves the document is derived, not typed).
- NFR harness: 7-day success rate, p95 staleness per tier, search p95 at **500k** rows,
  full-refresh wall time, closure/failure audit query.
- `docs/nfr.md` records actuals vs targets, with the query used for each number.

**Exit criteria:** every "working" row traces to a `fetch_run` id; every NFR prints a
measured value or an explicit "not yet met"; forecast-vs-measured divergence is stated
with a reason.

---

### S15 — Handoff package + final acceptance
**Deps:** S14 · **Model:** strongest

**Tasks**
- README: clean checkout → running system in ≤10 commands.
- `docs/runbook.md`: add/disable a source, read failures, respond to mass-failure,
  tune tiers and dedup thresholds, rotate credentials.
- `docs/cost-model.md`: arithmetic (requests/day, GB storage, headless minutes, DB,
  hosting) at 500k jobs / 6–24h refresh; what breaks if a dependency disappears.
- `docs/limitations.md`: top 5 fragility risks + mitigations; excluded platforms.
- `docs/adding-a-connector.md` final pass; template connector verified from scratch.
- Final acceptance run against all 8 acceptance criteria, with command output pasted.

**Exit criteria:** a person who has never seen the repo can run it from the README.

---

## 4. Model tiering

| Tier | Steps | Why |
|---|---|---|
| Strongest | S0, S2, S5, S6, S9, S15 | Schema, invariants, dedup and handoff are where silent correctness bugs live |
| Default | S1, S3, S4, S7–S8, S10–S14 | Pattern-following once the SDK and invariants exist |

Connector waves are deliberately default-tier: the SDK's job is to make them boring.
If a connector needs the strongest model, that is a signal the SDK is under-built.

---

## 5. Risks and mitigations

| Risk | Mitigation |
|---|---|
| 78 platforms becomes 78 half-stubs | Waves gated on a coverage artifact; unverified is an allowed state, "working" is not |
| Dedup over-merges and destroys trust | Precision gate 0.95, hard constraints, per-occurrence URLs preserved |
| Failed fetch closes jobs | S6 invariant suite, table-driven, blocking |
| Live-internet tests make CI flaky | S1 `no_network` fixture; S2 capture/replay |
| Schema churn after 20 connectors | Slice 1 validates shape before scale; `job`/`job_occurrence` split |
| ToS / blocking exposure | Structured-first policy, robots gate, identifiable UA, documented exclusions |
| 78 connectors ≠ 500k jobs | Tenant acquisition is its own problem (ADR 0005); the count is measured in S14, not assumed in the plan |
| Handoff cost | SDK + template + runbook + generated coverage, not prose claims |
| Scope creep into job-seeker features | Explicit out-of-scope list in README |

---

## 6. Plan mutation protocol

Steps may be split, inserted, skipped, or reordered only via a dated entry in
`plans/MUTATIONS.md` recording: what changed, why, which invariant or acceptance
criterion is affected, and whether the dependency graph changes. Silent reordering
is a process defect.

---

## 7. Approval gate

Approve §2 (wave sequencing) and the class assignments in `docs/platforms.md` before
S1 begins. Three things are most worth challenging now:

1. **Wave ordering** — enterprise suites (S10–S11) are 16 of 78 platforms but roughly
   half the engineering risk. Doing boards first would show breadth sooner but delay
   the hardest transport problem. Boards also feed S9's evaluation set, so they are
   not purely a breadth play.
2. **Platform inventory** — `docs/platforms.md` reconstructs the 78 from the named
   families in the brief. If your real list differs, correct that table before S1;
   it is the cheapest possible moment. Every row must keep exactly one `Wave`.
3. **Tenant acquisition** — ADR 0005 states the uncomfortable arithmetic: 78 platforms
   at one or two tenants each yields **~20k–50k** jobs, not 500k. Reaching the stated
   scale requires a tenant-discovery effort with its own cost. Approve the target tenant
   count (or lower the 500k figure) before S1, because it changes what "done" means.
