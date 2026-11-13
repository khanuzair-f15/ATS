# JobIndex

A single, deduplicated, searchable index of job postings harvested from ATS platforms
and job-board software platforms — built to stay current, to never lie about a closed
role, and to be operable by a team that did not write it.

**Status: Phase 0 decision set complete — awaiting approval. No runtime code yet.**
Nothing in this repository ingests jobs at this point, by design. Three things must be
approved before S1: the wave sequencing in the plan, the brief-exact platform inventory,
and the way scale targets are framed (platform coverage vs tenant discovery vs job
count). See §Next action below.

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
| `docs/adr/0005-tenants-and-secrets.md` | Platform coverage (A), tenant discovery (B), 500k benchmark (C), secret handling |
| `docs/schema.md` | `platform`, `source`, `job`, `job_cluster`, `job_occurrence`, `fetch_run`, `change_event` + invariants |
| `docs/platforms.md` | The brief's exact 78 platforms classified by transport, each owned by exactly one wave |
| `docs/brief-platforms.txt` | Frozen verbatim manifest: 58 ATS + 20 board platforms = 78 |
| `scripts/verify_phase0.py` | The inventory gate: manifest ↔ inventory, counts, waves, schema refs |

## Platform, tenant, connector, job

These four words are not interchangeable, and most of the scope confusion comes from
mixing them:

- **Platform** — an integration target named in the brief. There are **78 required**
  (58 ATS + 20 job-board software platforms). This is the contractual scope.
- **Tenant** — one employer career site or one board instance powered by a platform.
  One `source` row per tenant.
- **Connector** — one implementation per platform, parameterised by tenant. One
  connector should serve many tenants.
- **Job** — one posting observed at one tenant.

The 20 board platforms are **multi-tenant software** (Niceboard, WP Job Manager,
SmartJobBoard-class), not consumer job boards. LinkedIn, Indeed, Glassdoor,
ZipRecruiter, Monster, Dice, SimplyHired, Wellfound and YC are not in the brief and are
not in scope.

## Read in this order

1. `plans/jobindex-blueprint.md` §1 (invariants) — these are the rules that make the
   dataset trustworthy.
2. `docs/schema.md` §11 (closure state machine) — the requirement most job indexes get
   wrong.
3. `docs/adr/0005-tenants-and-secrets.md` §Context — why 78 platforms, 8–10k tenants and
   500k jobs are three separate objectives.
4. `docs/platforms.md` §3 — the forecast: 52 of 78 with live evidence, 26 named
   outstanding, 0 excluded.

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
   `outstanding` is an allowed state; overstated is not. A connector existing is never
   `verified`.

## Explicitly out of scope

- Job-seeker features: resumes, matching, alerts, in-app applications.
- Logged-in, paywalled, or auth-bypassed content.
- Consumer job boards not named in the brief (LinkedIn, Indeed, Glassdoor, and the rest).
- Distributed architecture: one region, one Postgres, one worker fleet.
- A production SPA. The search UI is server-rendered and minimal.

## Next action

Approve three things before S1 begins:

1. `plans/jobindex-blueprint.md` §2 — wave sequencing (`S3 → {S7, S10, S12} →
   {S8, S11} → S9 → S13 → S14`), with all 78 platforms owned by exactly one wave.
2. `docs/platforms.md` — the brief-exact 58 + 20 inventory. The names are frozen in
   `docs/brief-platforms.txt`; changing one is a scope change, not an edit.
3. `docs/adr/0005-tenants-and-secrets.md` §Decision — confirm the A/B/C framing:
   all 78 platforms are contractual, 8,000–10,000 tenants is a feasibility workstream,
   and 500k jobs is a non-contractual design benchmark.
