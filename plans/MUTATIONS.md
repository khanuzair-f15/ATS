# Plan mutations

Every change to wave sequencing, step scope, dependencies, or an accepted decision is
recorded here with its reason and its effect on invariants. Silent reordering is a
process defect (`plans/jobindex-blueprint.md` §6).

---

## M-001 — Phase 0 adversarial review remediation

- **Date:** 2026-11-13
- **Trigger:** adversarial architecture review of the Phase 0 package returned a
  **No-go** verdict: 12 critical, 13 major, 11 minor findings.
- **Scope:** documents only. No runtime code exists yet, which is why the whole set
  could be fixed before S1 instead of being deferred into migrations.
- **Dependency graph:** **changed.** `S9` now depends on `S7 + S12` (was `S7`); `S13`
  now depends on `S7, S8, S10, S11, S12` (was `S11, S12`); `S14` now depends on
  `S9 + S13` (was `S9, S12`). S2 gained the coverage generator.
- **Invariants:** 1, 2, 3 and 5 are strengthened. None weakened. No acceptance
  criterion was lowered.

### Critical findings

| ID | Finding | Resolution | Where |
|---|---|---|---|
| C1 | `job` had a random UUID and no natural key, so re-runs duplicated jobs | `UNIQUE (origin_source_id, origin_external_id)`; `job_occurrence` keyed `(source_id, external_id)` | `docs/schema.md` §5, §6 |
| C2 | No legal state for the losing row of a merge | `job_cluster` + `is_canonical` + partial unique index; losing jobs stay with occurrences intact | `docs/schema.md` §5 |
| C3 | Liveness trigger was on `job`, but occurrence updates never touch `job`, so it never fired | deferred constraint triggers on `job_occurrence` (`is_live`, `job_id`) and on `job.status` | `docs/schema.md` §8 |
| C4 | Absence-based closure had no defined start point or predicate | `job.stale_since_at`, `fetch_run.listing_complete`, and the closure predicate written as SQL | `docs/schema.md` §11 |
| C5 | `fetch_run` written at completion was lost entirely on a crash | written at **claim**, finalized at completion, `abandoned` outcome on heartbeat expiry | `docs/schema.md` §7, ADR 0003 |
| C6 | Queue uniqueness was vacuous (`gen_random_uuid()` per insert) | partial unique index on `source_id` for `pending`/`running` | `docs/schema.md` §10, ADR 0003 |
| C7 | 23 ATS rows had no owning step; S12 claimed 13 boards out of 11; four rows carried wrong transport facts (Taleo TBE/TEE endpoints swapped, Eightfold given Phenom's path, Teamtailor auth wrong) | `Wave` column on all 78 rows + §1a ownership table; counts asserted to sum to 78; transport facts corrected and unverified endpoints marked TBD | `docs/platforms.md` |
| C8 | Dedup (S9) was gated on S7 only, where cross-source overlap is near zero | S9 deps `S7 + S12`; eval set drawn from board-vs-ATS overlap | plan §2, S9 |
| C9 | `verified` was hand-edited until S14, i.e. a claim with no evidence | coverage generator ships in **S2**; `verified` is generated; S14 extends it | plan S2, S7, S14; `docs/platforms.md` |
| C10 | "500k jobs" was assumed to follow from 78 platforms; it does not | ADR 0005 states the arithmetic: ~8–10k tenants needed, discovery strategies, and that the number is measured in S14 | `docs/adr/0005-tenants-and-secrets.md` |
| C11 | Keyset pagination on `posted_at` silently drops NULL rows and is not a valid cursor | `sort_at = coalesce(posted_at, first_seen_at)` generated NOT NULL; `(sort_at, id)` cursor; matching indexes | `docs/schema.md` §5, ADR 0004 |
| C12 | S14 could run before the last connector wave, producing a premature coverage claim | S14 deps `S9 + S13` | plan §2, S14 |

### Major findings

| ID | Finding | Resolution | Where |
|---|---|---|---|
| M1 | S3 rollback said "delete the source row; occurrences cascade" — occurrences never cascade and must not be deleted | rollback is `source.enabled = false`; dev-only DB reset otherwise | plan S3 |
| M2 | `MAX_CLUSTER_SIZE` cap had no terminating algorithm | weight-ordered union-find with pre-union size check; rejected edges logged | ADR 0002 §6 |
| M3 | Blocking keys referenced columns that do not exist (`company_key`, `city_key`, `apply_host`) | keys restated on real columns; vacuous same-source key removed | ADR 0002 §2 |
| M4 | Veto rule 1 had no NULL semantics | veto fires only when all compared components are non-NULL; otherwise score capped at 0.85 | ADR 0002 §4 |
| M5 | Liveness guard was described but not implementable, and status/liveness ordering was unspecified | deferred constraint triggers, reopen path as one transaction | `docs/schema.md` §8 |
| M7 | Circuit breaker had no `degraded → healthy` transition | recovery transition added, plus per-`error_class` counter semantics | ADR 0003 |
| M8 | Replayed runs could duplicate `change_event` rows | `UNIQUE (occurrence_id, field, new_value, fetch_run_id)` | `docs/schema.md` §9 |
| M9 | Heartbeat/reclaim and the `abandoned` state path were unspecified | `heartbeat_at`, reclaim SQL, 3-attempt abandon rule, abandoned≠failure | ADR 0003, `docs/schema.md` §10 |
| M10 | Search ADR used a non-existent `salary_min_normalized`, mixed 100k/500k scales, and `search_tsv` had two definitions | `salary_min`; 500k as the single scale; column defined once in schema, ADR indexes it | ADR 0004 |
| M13 | `platform` registry did not exist, so class/wave lived only in Markdown and coverage could not be DB-backed | `platform` table seeded from `docs/platforms.md`; drift test | `docs/schema.md` §3 |

The remaining major findings — **M6, M11, M12** — were documentation-consistency
items (stale cross-references, an implicit S1 table set, inconsistent table names such
as `dedup_candidate_queue` vs `dedup_candidate`). They are fixed in place by the edits
listed under the minor findings and in the plan/schema diffs above, which is why they
have no separate row here. No major finding was dropped.

### Minor findings

| ID | Finding | Resolution |
|---|---|---|
| m1 | ADR 0003 cross-referenced "ADR-0002-era rules" for closure | now cites the S6 suite and `docs/schema.md` §11 |
| m2 | A job could only keep one apply URL | `job_occurrence.apply_urls text[]` + generated `apply_url` |
| m3 | `description` nullable in places where it is always required | `NOT NULL DEFAULT ''` on both columns |
| m4 | `next_due_at` was computed non-sargably | materialized column + partial index |
| m5 | `format: email`-style enum drift | enums centralized in `docs/schema.md` §2 |
| m6 | Queue `state` index did not match the claim query | partial index on `available_at WHERE state='pending'` |
| m7 | S1's table set was implicit, inviting a rewritten baseline migration | explicit S1 table set in `docs/schema.md` §13 and plan S1 |
| m8 | `apply_url` had no primary/tie-break rule | `apply_urls[1]` is the primary, documented |
| m9 | Taleo endpoints were swapped (TBE vs TEE) | corrected |
| m10 | Phenom and Eightfold shared an identical endpoint claim | Eightfold marked TBD; probe-first |
| m11 | README's schema section reference was stale | corrected to §11 |

### Accepted and deferred (deliberately not fixed in Phase 0)

These are **not** oversights. Each is deferred to a named step because it cannot be
resolved without runtime evidence.

| Item | Why deferred | Resolved by |
|---|---|---|
| Live probe of every platform endpoint | Requires network access and real tenants; Phase 0 has no runtime | S2 capture tool, then each wave's probe-first rule |
| Which C2 rows demote to C3 | Depends on probing | S8/S10/S11 demotion rule |
| Dedup thresholds and precision number | Must come from a ≥400-pair labeled set | S9, ADR 0002 stays `Proposed` |
| The actual tenant count and job count | Depends on the acquisition job | ADR 0005 mechanism; measured in S14 |
| Cost model figures | Depends on measured tenant count and request volume | S15 `docs/cost-model.md` |
| PR/CI automation | `gh` is absent; direct mode is the accepted workflow | not planned; commits are the review artifact |
| Full NFR numbers | Need 7 days of production-shaped runs | S14 `docs/nfr.md` |

### Verification of this mutation

- `docs/platforms.md`: 78 data rows, 78 distinct waves-owners, wave counts sum to 78,
  class counts match the stated distribution.
- `docs/schema.md` v2 is the single source for identity, liveness, closure and the S1
  table set; no other document redefines those.
- ADR 0002 remains `Proposed`; ADR 0005 is `Proposed` pending tenant-count approval.
  ADRs 0001, 0003, 0004 are `Accepted`.
- The approval gate in plan §7 now names three things to approve: wave sequencing,
  platform inventory, tenant-count target.

---

## M-002 — findings caught while verifying M-001

- **Date:** 2026-11-13
- **Trigger:** re-running the consistency checks over the remediated documents rather
  than trusting the edit list.
- **Scope:** documents only; no dependency or scope change.

| ID | Finding | Resolution | Where |
|---|---|---|---|
| V1 | `job_cluster.canonical_job_id` and `job.is_canonical` could disagree, producing a cluster with **zero** canonical rows — a job silently absent from `/v1/jobs`, which is worse than a duplicate. The partial unique index constrains two canonical rows, not zero. | `canonical_job_id` named the authority; `is_canonical` a maintained denormalization; deferred constraint trigger `cluster_has_canonical` asserts the cluster has a canonical member at commit | `docs/schema.md` §5 |
| V2 | `job_cluster.canonical_job_id` and `job.cluster_id` are mutually referential, and no write order was documented — the obvious "insert cluster first" order cannot work | documented the only valid three-step order in one transaction | `docs/schema.md` §5 |
| V3 | ADR 0003's Enqueue section said "two statements" and showed three; the schedule-advance statement also advanced `open` sources, delaying a half-open probe by up to a full tier interval | three statements, fixed order, `health <> 'open'` excluded from the advance | `docs/adr/0003-scheduling-and-queue.md` |
| V4 | ADR 0005's reversal cost implied `source` rows may be deleted, contradicting the no-delete rule that protects occurrences | `candidate`/`rejected` rows (no occurrences) may be disabled; a `validated` source is disabled, never deleted | `docs/adr/0005-tenants-and-secrets.md` |
| V5 | Major findings were numbered M1–M13 with M6, M11, M12 absent from the table, which reads as three dropped findings | stated explicitly that they were the documentation-consistency items, fixed in place, none dropped | this file |
| V6 | README said "two things must be approved" while the gate lists three | corrected to three, self-reference fixed | `README.md` |
| V7 | Plan §7 quoted "~10–20k jobs" at 1–2 tenants per platform while ADR 0005 derives ~20k–50k; two documents, two answers to the same arithmetic | plan now quotes ADR 0005's figure | plan §7 |

**Verification of M-002:** the platform-count script reports 78 data rows, class counts
`{C1: 11, C2: 31, C3: 18, C4: 9, EX: 9}`, wave counts
`{S3: 1, S7: 10, S8: 15, S10: 8, S11: 8, S12: 9, S13: 18, EX: 9}`, no blank or multi-wave
cell, no duplicate platform name, no row claiming `Verified` ≠ `no`; step `Deps` lines
match plan §2; every `docs/schema.md` §-reference resolves to an existing section.
