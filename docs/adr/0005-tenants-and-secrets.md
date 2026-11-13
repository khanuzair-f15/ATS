# ADR 0005 — Platform coverage, tenant discovery, and secret handling

- **Status:** Accepted (mechanism). The figures in objectives **B** and **C** are
  targets, not commitments.
- **Date:** 2026-11-13
- **Deciders:** project owner (scope and targets), engineering (mechanism)
- **Affects:** S1 (schema, constraints), S2 (capture redaction), S5 (scheduler),
  S12 (board wave), S14 (coverage + NFR), S15 (runbook, cost model)

## Context

Three different things were being treated as one number. They have different
authorities, different owners, and different failure modes:

| Term | Definition |
|---|---|
| **Platform** | An integration target named in the brief. The brief requires **58 ATS + 20 job-board software platforms = 78**. |
| **Tenant** | One employer career site or one board instance powered by a platform. A platform has many tenants; `source` is one row per tenant (`docs/schema.md` §4). |
| **Connector** | One implementation per platform, parameterised by tenant. One connector should serve every tenant of that platform wherever the transport allows it. |
| **Job** | One posting, observed at one tenant. |

**Platform support is not recognition.** A platform is supported when a connector has
indexed real postings from real tenants, not when the platform has been identified,
classified, or parsed once from a sample page.

Two facts are easy to conflate and expensive to confuse:

1. There are **78 required platforms**. That is the contractual scope.
2. **~500k jobs** was a scale target used to size the design. It is not a promise and
   not the definition of success.

### The arithmetic that must never become a promise

A connector is not a dataset. A Greenhouse connector does not contain Greenhouse jobs;
it contains the jobs of the specific board tokens we point it at.

| Assumption | Value |
|---|---|
| Tenants run at 1–2 per platform | ~100–200 |
| Median open roles per ATS tenant | ~40 (observed range 5–300; assumption) |
| Jobs from that tenant set | **~4k–8k** |
| Jobs from structured boards | ~20k–50k, heavily overlapping ATS (reposts dominate) |
| **Total at 1–2 tenants per platform** | **~20k–50k** |

So "78 platforms" mechanically yields **tens of thousands** of jobs. The gap to 500k is
roughly **10×**, and it is not closed by writing more connectors — it is closed by
running thousands of tenants through connectors that already exist.

That is why the three objectives below are separate. Collapsing them is how a project
ends up claiming a job count it never measured.

## Decision

**Keep three objectives separate, with different authority, and state which one wins
when they conflict.**

| # | Objective | Type | Authority | What failure means |
|---|---|---|---|---|
| **A** | Working coverage across all **78 required platforms** | **Contractual** | the brief | A platform without live indexing is **undelivered and named**. This is the only acceptance failure. |
| **B** | Discover and index **8,000–10,000 tenant instances** | **Feasibility exploration**, separate workstream | project owner | A measured shortfall is a **finding to report**, not a breach. |
| **C** | **500,000 indexed jobs** | **Design-scale benchmark**, non-contractual | engineering | Not a breach under any outcome. The measured number is reported instead. |

**Priority rule: A outranks C.** Platform completeness is the deliverable. Chasing a
job-count milestone by quietly dropping hard platforms is the failure this ADR exists to
prevent. B serves both A and C and is reported honestly against neither.

### A. Mandatory acceptance target — all 78 platforms

Coverage is reported in four states. They are **not** interchangeable, and the coverage
report must use these words:

| State | Meaning | Required evidence |
|---|---|---|
| `implemented` | Connector code exists and merges into the pipeline | code + a fixture-backed test |
| `tested` | The offline fixture suite passes for this connector | CI result, no network |
| `verified` | Real postings indexed from a real tenant | `fetch_run` row + `job`/`job_occurrence` rows + the tenant identifier recorded |
| `outstanding` | No live indexing yet | named in the coverage report with a reason and an owner |

Rules that follow from this:

- **A connector existing is never `verified`.** Only a live run with stored evidence is.
- **No required platform may be marked excluded.** If a platform turns out to be
  technically blocked, it stays in the inventory as `outstanding` with a named reason
  and a revisit date. It does not leave the scope, and it is never silently dropped.
- Every required platform carries exactly one implementation wave in
  `docs/platforms.md`. The wave counts must sum to 78.
- Coverage arithmetic is reported as `verified + outstanding = 78`, never as a
  percentage that hides which platforms are missing.

> **Current state:** the inventory in `docs/platforms.md` is **v1.0 — brief-exact**
> (58 ATS + 20 board software platforms, frozen in `docs/brief-platforms.txt`, checked
> row-for-row by `scripts/verify_phase0.py`). Objective A is therefore assessable from
> this point. The correction that replaced the earlier reconstruction is M-003 in
> `plans/MUTATIONS.md`.

### B. Tenant discovery target — 8,000–10,000 tenants

This is a **feasibility exploration run as its own workstream**, not a connector
milestone and not a promise.

| Tenant count | Mean jobs/tenant | Implied jobs |
|---|---|---|
| 1,000 | 60 | 60k |
| 5,000 | 60 | 300k |
| **8,000–10,000** | **50–65** | **400k–650k** |

The arithmetic above is *hypothesis*, built from an assumed mean. It exists to show the
order of magnitude and to justify the discovery workstream. It is not evidence, and it
is not quoted as a result.

#### Discovery strategies, ordered by yield per unit of engineering

| Strategy | Mechanism | Yield | Notes |
|---|---|---|---|
| **Board-mined** (primary) | Fetch board postings → extract every apply URL → classify its host as an ATS tenant → insert `source` with `discovered_via='board-mined'` | High. Every board posting is a pointer to a real employer ATS tenant | Needs the board wave; runs continuously; the tenant set grows with each board refresh |
| **Slug probing** | For a curated company-domain list, probe predictable tenant endpoints and keep 200s with ≥1 job | High for structured platforms; one request per candidate | 404 is the expected answer, not an error. Per-host concurrency 1, identifiable UA, offline batch job |
| **Careers-path crawl** | For a known employer domain: `robots.txt` → `sitemap.xml` → careers URL → identify vendor from redirect/embedded script | Medium | Robots-respecting; structured hosts only, never headless-only platforms |
| **Manual seeding** | Named enterprise tenants | Low count, very high jobs/tenant | Best jobs-per-source ratio; do this first for headline numbers |
| **Registry import** | Public vendor customer directories / host lists | Medium, variable quality | Checked against robots/ToS before use |

#### Guardrails

- Probing is an **offline batch job**, never part of a refresh run, and never shares a
  rate-limit budget with connectors.
- A `source` row starts as `tenant_status = 'candidate'` and becomes `validated` only
  when a real fetch returns ≥1 job and a `fetch_run` row.
- A candidate returning 0 jobs on three consecutive validated attempts becomes
  `rejected` and stops being scheduled. It is not deleted.
- `discovered_via` is recorded for every row, so a bad discovery vector can be audited
  and reversed in bulk.
- `(platform_key, tenant_key)` is UNIQUE, so re-discovery is idempotent.

#### What B reports

`tenants_discovered`, `tenants_validated`, `tenants_rejected`, and the **distribution**
of jobs per validated tenant (mean, median, p90). A validated count below 8,000 is
reported as a finding with its cause. It is not rounded up, and it does not retroactively
change A.

### C. Scalability benchmark — 500,000 indexed jobs

- The schema, search and queue are **designed so 500k is not a redesign**: FTS +
  `pg_trgm` indexes (ADR 0004), keyset pagination on `sort_at`, `FOR UPDATE SKIP LOCKED`
  queue (ADR 0003), no per-job external calls.
- S14 seeds **500k synthetic rows** for the latency benchmark. That measures the design.
  It says nothing about the corpus.
- **No claim is made that 8,000–10,000 tenants will produce 500k jobs.** Extrapolation is
  permitted only from validated measurement:
  `projected = validated_tenants × measured_mean_jobs_per_tenant`, reported with its
  interval and the observed distribution beside it.
- If the measured corpus is 60k, the report says 60k. A benchmark that the corpus does
  not reach is a scale statement about the system, not a failure of the delivery.

### D. Secret handling

Only a small number of rows need a credential. Everything else is public and
unauthenticated by policy.

- **`source.config` never holds a secret.** Enforced by `config_no_secrets` in
  `docs/schema.md` §4. Config holds only non-sensitive tenant parameters (site id,
  locale, board path).
- **Resolution:** a worker resolves a secret by `source.id` at startup from exactly one
  of:
  1. environment variable `JOBINDEX_SECRET_<source_id_with_dashes_as_underscores>`, or
  2. a file at `$JOBINDEX_SECRET_DIR/<source_id>` (mode 0600).
- **Never persisted:** not written to the DB, not included in `fetch_run.error_detail`,
  not logged, not committed. Backups and dumps are therefore safe to hand to an acquirer.
- **Capture tool redaction (S2):** `tools/capture.py` strips `Authorization`, `Cookie`,
  `Set-Cookie`, `X-Api-Key` and any query parameter matching `token|key|secret` before
  writing a fixture, and records that it did so in the fixture manifest.
- **Tests:** one asserts no `source.config` has a forbidden key; another scans
  `tests/fixtures/**` for credential-looking strings and fails the build.
- **Rotation:** update the env var or file and restart the worker. No schema change, no
  row update.
- **A missing secret is a `permanent` error for that source**, not a retry loop. The
  source goes to `open` with a named reason and appears as `outstanding` in coverage.

## What "done" means

S14 reports, and the acceptance conversation is about these three blocks — never about a
single headline number:

1. **Per platform:** the four-state coverage table for all 78, with `verified +
   outstanding = 78`.
2. **Per corpus:** `jobs_open`, `jobs_new_last_7d`, `jobs_closed_last_7d`, measured
   refresh wall time at the acquired tenant count.
3. **Per tenant:** discovered / validated / rejected, and the jobs-per-tenant
   distribution.

"78 connectors exist" is not a coverage claim. "500k jobs" is not a coverage claim. Only
the three blocks above are.

## Consequences

**Positive**

- The contractual scope (A) can be met and audited independently of the scale outcome
  (B, C), so a small measured corpus cannot be mistaken for a failed delivery, and a
  large corpus cannot hide a missing platform.
- The platform/tenant split is already in the schema, so scaling is an operational
  action (add tenant rows) rather than a code change.
- Board-mined discovery turns the board wave into a tenant-acquisition engine, so the
  board work pays for itself twice.
- No secret in the database means backups, fixtures and the handoff repo are safe by
  construction.

**Negative**

- Three objectives means three reports; a reader looking for one number will not find it.
  That is the point.
- Tenant discovery is real engineering with real request volume; it is not free breadth.
- Thousands of tenants multiply freshness work: the scheduler scales by tenant count, and
  the 6–24h window is a function of that count.
- More tenants means more duplicate postings, which raises the importance of S9 and its
  precision gate.
- Slug probing is indistinguishable from enumeration if done carelessly. It must stay
  rate-limited, identifiable, and confined to documented public endpoints.

## Reversal cost

Low. Tenant rows are data, not architecture: disabling a `candidate` or `rejected` row
has no schema impact, and those states have no occurrences by definition. A `validated`
source that has ever produced jobs is **disabled, never deleted** — deleting it would
destroy the occurrences that back its history. Secret resolution is a single module
behind an interface; moving to a managed secret store (Vault, SSM, cloud KMS) changes
that module and nothing else.
