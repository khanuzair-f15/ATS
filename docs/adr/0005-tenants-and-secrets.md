# ADR 0005 — Tenant acquisition and secret handling

- **Status:** Proposed — the target tenant count must be approved with the plan
- **Date:** 2026-11-13
- **Deciders:** project owner (scale target), engineering (mechanism)
- **Affects:** S1 (schema, constraints), S2 (capture tool redaction), S5 (scheduler),
  S14 (measured coverage), S15 (runbook, cost model)

## Context

Two facts are easy to conflate and expensive to confuse:

1. There are **78 source platforms** (58 ATS + 20 boards).
2. The target is **~500k active jobs**.

Also this constraint, already in the schema: **`source` is one row per tenant, not per
platform** (`docs/schema.md` §4), and `source.config` must never contain a secret
(`config_no_secrets` check constraint).

### The arithmetic problem

A connector is not a dataset. A Greenhouse connector does not contain Greenhouse jobs;
it contains the jobs of the specific board tokens we point it at.

| Assumption | Value | Source |
|---|---|---|
| Active ATS tenants we would run at 1–2 per platform | ~100–200 | plan as written before this ADR |
| Median open roles per ATS tenant | ~40 | observed range 5–300; assumption |
| Jobs from that tenant set | **~4k–8k** | 100–200 × 40 |
| Jobs from 9 structured boards | ~20k–50k, heavily overlapping ATS | board volume is dominated by reposts |
| **Total at 1–2 tenants per platform** | **~20k–50k** | |

So "78 platforms" mechanically yields **tens of thousands** of jobs, not 500k. The gap
is roughly **10×**, and it is not closed by writing more connectors — it is closed by
running **thousands of tenants** through the connectors we already have.

| Tenant count | Mean jobs/tenant | Jobs |
|---|---|---|
| 1,000 | 60 | 60k |
| 5,000 | 60 | 300k |
| **8,000–10,000** | **50–65** | **400k–650k** |

Reaching the stated scale therefore needs **roughly 8,000–10,000 active ATS tenants**,
plus boards. That is a tenant-discovery program, not a connector program, and it has
its own cost, rate-limit exposure and freshness implications.

### What this ADR does not claim

It does **not** promise 500k jobs in the delivery. It says: the schema, search and
queue are designed for 500k; the acquired tenant count is a **measured output** in S14;
and if the measured number is 60k, the coverage report says 60k. The 500k figure is a
scale target for design, not a claim about the harvest.

## Decision

**Separate `platform` (78 rows, a registry) from `source` (thousands of tenants), and
acquire tenants with an offline, rate-limited discovery job whose output is `candidate`
rows. Secrets are resolved from the environment by `source.id` and never stored in the
database.**

### 1. Tenant acquisition strategies

Ordered by expected yield per unit of engineering:

| Strategy | Mechanism | Yield | Notes |
|---|---|---|---|
| **Board-mined** (primary) | Fetch C4 board jobs → extract every apply URL → classify its host as an ATS tenant → insert `source` with `discovered_via='ats-slug'` | High. Every board job is a free pointer to a real employer ATS tenant | Requires only the S12 boards. Runs continuously; the tenant set grows with each board refresh |
| **Slug probing** | For a curated company-domain list, probe the predictable tenant endpoints (`boards-api.greenhouse.io/v1/boards/{slug}/jobs`, `api.lever.co/v0/postings/{slug}`, `api.ashbyhq.com/posting-api/job-board/{slug}`, `apply.workable.com/api/v1/widget/accounts/{slug}`) and keep 200s with ≥1 job | High for C1; cheap (one request per candidate) | 404 is the expected answer, not an error. Per-host concurrency 1, identifiable UA, one-off offline job |
| **Careers-path crawl** | For a known employer domain: `robots.txt` → `sitemap.xml` → careers URL → identify ATS vendor from redirect/embedded script | Medium | Robots-respecting; only for C1/C2 hosts, never C3 |
| **Manual seeding** | Named enterprise tenants (Workday, Taleo, SAP, ORC) | Low count, very high jobs/tenant (hundreds–thousands) | Best jobs-per-source ratio; do this first for headline numbers |
| **Registry import** | Public ATS customer directories / Common Crawl host lists | Medium, variable quality | Must be checked against robots/ToS before use |

**Guardrails for all strategies**

- Probing is an offline batch job, not part of a refresh run. It never shares a
  rate-limit budget with a connector run.
- A `source` row starts at `tenant_status = 'candidate'` with `expected_min_jobs = 1`.
  It becomes `validated` only when a real fetch returns ≥1 job and a `fetch_run` row.
- A candidate returning 0 jobs on three consecutive validated attempts becomes
  `rejected` and stops being scheduled. It is **not** deleted (occurrences, if any,
  survive).
- `discovered_via` is recorded for every row so a bad discovery vector can be audited
  and reversed in bulk.
- `tenant_key` is the board slug/path; `(platform_key, tenant_key)` is UNIQUE, so
  re-discovery is idempotent.

### 2. Secret handling

Only a small number of rows need a credential: Comeet (`token`), Teamtailor's JSON API
(`token`), and any partner-gated C4 row a stakeholder licenses. Everything else is
public and unauthenticated by policy.

- **`source.config` never holds a secret.** Enforced by `config_no_secrets` in
  `docs/schema.md` §4. Config holds only non-sensitive tenant parameters (site id,
  locale, board path).
- **Resolution:** a worker resolves a secret by `source.id` at startup from exactly one
  of:
  1. environment variable `JOBINDEX_SECRET_<source_id_with_dashes_as_underscores>`, or
  2. a file at `$JOBINDEX_SECRET_DIR/<source_id>` (mode 0600).
- **Never persisted:** secrets are not written to the DB, not included in
  `fetch_run.error_detail`, not logged, and not committed. Backups and dumps are
  therefore safe to hand to an acquirer.
- **Capture tool redaction (S2):** `tools/capture.py` strips `Authorization`, `Cookie`,
  `Set-Cookie`, `X-Api-Key` and any query parameter matching `token|key|secret` before
  writing a fixture, and records that it did so in the fixture manifest.
- **Tests:** one test asserts no `source.config` has a forbidden key; another scans
  `tests/fixtures/**` for credential-looking strings and fails the build.
- **Rotation:** update the env var or file and restart the worker. No schema change, no
  row update, no downtime beyond that worker.
- **A missing secret is a `permanent` error for that source**, not a retry loop. The
  source goes to `open` with a named reason in the coverage report.

### 3. What "done" means for scale

S14 reports, per platform and in total:

- `tenants_discovered`, `tenants_validated`, `tenants_rejected`
- `jobs_open`, `jobs_closed_last_7d`, `jobs_new_last_7d`
- measured refresh wall time at the acquired tenant count

The acceptance conversation is about those numbers. "78 connectors exist" is not a
scale claim and must never be presented as one.

## Consequences

**Positive**

- The platform/tenant split is already in the schema, so scaling is an operational
  action (add tenant rows) rather than a code change.
- Board-mined discovery turns the board wave into a tenant-acquisition engine, which
  makes S12 pay for itself twice.
- No secret in the database means backups, fixtures and the handoff repo are safe by
  construction.

**Negative**

- Tenant discovery is a real engineering task with real request volume; it is not free
  breadth.
- Thousands of tenants multiply freshness work: the scheduler must scale by tenant
  count, not platform count, and the 6–24h window is a function of that count.
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
