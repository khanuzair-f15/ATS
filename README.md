# JobIndex

A single, deduplicated, searchable index of job postings harvested from ATS platforms
and job boards — built to stay current, to never lie about a closed role, and to be
operable by a team that did not write it.

**Status: Phase 0 decision set complete — awaiting approval. No runtime code yet.**
Nothing in this repository ingests jobs at this point, by design. Three things must be
approved before S1: the wave sequencing in the plan, the platform inventory in
`docs/platforms.md`, and the tenant-count target behind the ~500k figure. See
§Next action below.

---

## What this repo currently contains

| Path | What it decides |
|---|---|
| `plans/jobindex-blueprint.md` | The build plan: 16 steps, dependency graph, per-wave gates, risks |
| `plans/MUTATIONS.md` | Every change to the plan, with reasons and invariant impact |
| `docs/adr/0001-tech-stack.md` | Python/FastAPI/Postgres/Playwright — options compared, reversal cost |
| `docs/adr/0002-deduplication.md` | Blocking keys, similarity, precision-first thresholds, eval protocol |
| `docs/adr/0003-scheduling-and-queue.md` | Postgres `SKIP LOCKED` queue, refresh tiers, circuit breaker |
| `docs/adr/0004-search.md` | Postgres FTS + `pg_trgm`, ranking, measured triggers to revisit |
| `docs/adr/0005-tenants-and-secrets.md` | Why 78 platforms ≠ 500k jobs, tenant discovery, secret handling |
| `docs/schema.md` | `platform`, `source`, `job`, `job_cluster`, `job_occurrence`, `fetch_run`, `change_event` + invariants |
| `docs/platforms.md` | All 78 platforms classified by transport, each owned by exactly one wave |

## Read in this order

1. `plans/jobindex-blueprint.md` §1 (invariants) — these are the rules that make the
   dataset trustworthy.
2. `docs/schema.md` §11 (closure state machine) — the requirement most job indexes get wrong.
3. `docs/adr/0005-tenants-and-secrets.md` §Context — the arithmetic behind the 500k target.
4. `docs/platforms.md` §3 — what is realistically deliverable and what is explicitly out.

## The three rules that shape everything

1. **A failed fetch is never evidence that a job closed.** Only 404/410, an explicit
   closed marker, or absence from a *successful, complete* listing may close a job.
   Timeouts, 429s, 5xx and partial listings never do. This is enforced by DB
   constraints, a trigger, and a table-driven test suite.
2. **Every source URL survives.** Deduplication merges canonical fields; it never
   deletes an occurrence. A merged job still shows every place it was seen and every
   apply link.
3. **Nothing is claimed working without evidence.** A platform is `verified` only when
   a connector produced real jobs from a real tenant and a `fetch_run` row proves it.
   Unverified is an allowed state; overstated is not.

## Explicitly out of scope

- Job-seeker features: resumes, matching, alerts, in-app applications.
- Logged-in, paywalled, or auth-bypassed content.
- Distributed architecture: one region, one Postgres, one worker fleet.
- A production SPA. The search UI is server-rendered and minimal.

## Next action

Approve three things before S1 begins:

1. `plans/jobindex-blueprint.md` §2 — wave sequencing (`S3 → S7 → S8 / S10 → S11 /
   S12 → S13 → S14`).
2. `docs/platforms.md` — correct any of the 78 rows that are not on your real list.
3. `docs/adr/0005-tenants-and-secrets.md` §Context — confirm the tenant-count target
   behind the ~500k figure, or lower the figure.
