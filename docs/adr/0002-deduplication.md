# ADR 0002 — Deduplication strategy

- **Status:** Proposed — thresholds are **provisional** and must be replaced by measured
  values in S9 before this ADR is marked Accepted. Acceptance requires a holdout
  precision lower bound ≥ 0.95 (§Evaluation protocol).
- **Date:** 2026-11-13
- **Affects:** S1 (schema support), S9 (engine + evaluation), S14 (coverage)

## Context

The same opening routinely appears on a company's own careers page, on two ATS-hosted
boards, and on a job board. Titles are reworded, locations are formatted differently,
salaries are encoded in five ways, and descriptions carry boilerplate. The index must
present one canonical record while preserving every source link.

Two failure modes, not one:

- **Under-merging** (recall failure) — the user sees duplicates. Annoying, visible,
  survivable.
- **Over-merging** (precision failure) — two genuinely distinct requisitions
  ("Backend Engineer, Payments" and "Backend Engineer, Platform" at the same company)
  collapse into one record, and the index silently loses a job. **This is the
  dangerous failure**: it is invisible, it corrupts counts, and it is what makes an
  acquirer distrust the dataset.

The policy is therefore **precision-first**: a missed merge is acceptable, a wrong
merge is not.

## Options considered

| Option | Verdict |
|---|---|
| Exact key match only (company + normalized title + city) | Too weak. Misses reworded titles; no partial credit. Cheap baseline to measure against. |
| Full pairwise comparison of all jobs | O(n²). At 500k jobs that is 1.25×10¹¹ comparisons. Rejected. |
| Blocking keys + fuzzy similarity (chosen) | Sub-quadratic, tunable, explainable, cheap to run on Postgres + Python. |
| Embedding similarity over descriptions | Optional enhancer only. Costs an inference dependency per job, is hard to explain to an operator, and must beat the cheap method on the eval set to be adopted. |
| LLM adjudication of candidate pairs | Not adopted as primary. Possible narrow use as a tie-breaker on the highest-uncertainty band, capped by cost. Must be measured, not assumed. |

## Decision

**Blocking keys → cheap similarity features → hard constraints → thresholded
union-find clustering.**

### 1. Normalization (shared, deterministic, versioned)

`dedup_version` is stored on every cluster so a policy change is auditable and
re-runnable.

- title: lowercase, strip seniority noise where safe (`(m/f/d)`, `- remote`, req ids),
  strip punctuation, singularize a small controlled vocabulary, collapse whitespace,
  remove company name if embedded.
- company: canonical domain when known (from apply URL or source tenant), else
  normalized name with legal suffixes stripped (`inc`, `gmbh`, `ltd`, `bv`, …).
- location: reuse the `job` location fields (`city_key`, `region_key`, `country_code`);
  keep `raw` for display only, never for matching.
- description: HTML-stripped, lowercased, boilerplate blocks removed (EEO statements,
  "apply now", cookie notices), tokenized, 5-word shingles.

### 2. Blocking keys (candidate generation)

A pair is compared only if it shares **at least one** key:

| Key | Definition | Catches |
|---|---|---|
| `B1` | `(company_domain, title_stem)` | same employer, reworded title |
| `B2` | `(company_normalized, city_key)` | same employer + city, different title phrasing |
| `B3` | `(apply_host, external_id)` | the same posting proxied through two sources |
| `B4` | `(company_domain, apply_host)` | same req cross-posted to a board |

`title_stem` = normalized title with department/track qualifiers kept and formatting
removed. `company_domain`, `company_normalized`, `city_key` and `apply_host` are the
actual columns in `docs/schema.md` §5 — no key references a column that does not exist.
Keys with a NULL component are skipped, not coalesced: a null city must not block every
remote job together.

`B3` fires only **across** sources. Within one source, `(source_id, external_id)` is
already a UNIQUE constraint, so two rows cannot exist — that pair is an upsert, not a
dedup candidate. A blocking key that only ever matches rows that cannot coexist is
vacuous and was removed.

Expected candidate volume: comparisons are `Σ_k C(n_k, 2)` over blocking groups, not a
fraction of all pairs. At the measured group sizes this is several orders of magnitude
below `n²/2` at 500k jobs, and it is a batch job measured in minutes on one process pool.

### 3. Similarity features

| Feature | Method | Weight |
|---|---|---|
| Title | `rapidfuzz` token set ratio on normalized titles | high |
| Company | exact domain match / token set ratio on name | high |
| Location | city equal / country equal / one side unknown | medium |
| Description | trigram Jaccard **or** MinHash-estimated Jaccard on 5-word shingles | medium |
| Employment type | enum equality | low |

A weighted score in `[0,1]` plus a per-feature evidence record stored on the pair
(so a disputed merge can be explained without re-running).

### 4. Hard constraints (veto rules)

Regardless of score:

- **Veto 1 — different req.** Different `apply_host` **and** different `external_id`
  **and** different `city_key` ⇒ **no merge** unless description Jaccard ≥
  `HARD_DESC_SIM = 0.80`.
- **Veto 2 — same board, different reqs.** Two jobs with non-null, different
  `external_id` values originating from the same `source` ⇒ **no merge**.
- **Veto 3 — region-locked remote.** Both `remote_status = 'remote'` and
  `country_code` both non-null and different ⇒ no merge unless description Jaccard
  ≥ `HARD_DESC_SIM`.

**NULL semantics (explicit).** A veto only applies when **every** component it
compares is non-NULL. If any component is NULL the veto does not fire, and the pair is
instead **capped at score 0.85** — below the auto-merge band — so it can be surfaced
for review but can never be merged automatically. "Unknown city" must not be treated
as "different city" (that silently blocks true duplicates) and must not be treated as
"same city" (that silently merges distinct reqs). Capping is the only safe third
option.

### 5. Thresholds

Provisional starting grid, to be replaced in S9 by a sweep on the labeled set:

| Band | Score | Action |
|---|---|---|
| auto-merge | ≥ 0.88 | merge |
| review | 0.72 – 0.88 | **not merged automatically.** Logged to `dedup_candidate` with its feature evidence; merged only after an explicit human approval that records who approved it |
| no-merge | < 0.72 | separate |

The review band is a queue, not a weaker merge rule. There is exactly one automatic
merge path and it is the auto-merge band; anything else requires a human. Earlier
wording said both "merge if hard constraints pass" and "not auto-merged", which was
contradictory — the queue interpretation is the decision.

### 6. Clustering

Union-find over accepted pairs, processed **in descending score order** (a
maximum-spanning-forest construction):

1. Sort accepted pairs by score, highest first.
2. For each pair `(u, v)`: find component roots (path compression). If equal, skip.
3. If `size(root_u) + size(root_v) > MAX_CLUSTER_SIZE` (**default 6 occurrences**),
   **reject the edge** and log it to `dedup_rejected_edge` with its score and reason.
4. Otherwise union by size (smaller component into larger), recording the edge.

This bounds every component at `MAX_CLUSTER_SIZE` by construction. A naive
"merge, then check size, then split by dropping the weakest edge" does not terminate
cleanly and can split the wrong edge; processing edges in weight order with a
pre-union size check makes the cap exact and the result deterministic for a given
`dedup_version`. The strongest evidence is always the evidence that survives the cap.

`job.cluster_id` points at the canonical cluster. `job_occurrence` rows are never
merged or deleted — the occurrence table is the audit trail and the source of
"also seen on".

Canonical-field selection inside a cluster: prefer the occurrence with the most
complete record (description length, salary present, apply URL reachable), tie-broken
by earliest `first_seen_at`. The chosen record's `content_hash` drives change events.

## Evaluation protocol (S9, mandatory)

The gate is a **statistical** claim, not a point estimate. A precision of 19/20 is
compatible with a true precision near 0.75; shipping that as "0.95" is exactly the
overstatement this project forbids.

- **≥400 labeled true-duplicate pairs** and **≥400 labeled hard negatives**, drawn from
  real captured data after Wave E (S12), not invented:
  - true duplicates: cross-source same job, reworded titles, reposted-with-edits
  - hard negatives: same title/different city, same title/different team, same
    company/different req id, same board/different posting, repost with a different req
- Labels stored in `tests/dedup/labeled_pairs.jsonl` with the reason for each label and
  how the pair was found.
- **30% stratified holdout** by `(source, employer)` pair, held out from **all**
  threshold tuning. The reported number is computed on the holdout only, once.
- Gate: **Wilson 95% lower bound on precision ≥ 0.95** on the holdout. If the lower
  bound is below 0.95, thresholds do not ship and this ADR stays `Proposed`.
- Recall is reported with its own Wilson interval. It is reported honestly and is
  **not** gated — precision-first is the policy.
- Report the full threshold sweep on the tuning split, a confusion breakdown by
  feature (which features cause false merges), per-blocking-key recall (drop a key that
  contributes nothing), and a named failure mode with one example each.

## Consequences

**Positive:** explainable merges, sub-quadratic cost, no new infrastructure, thresholds
change without a schema migration, every source URL preserved.

**Negative:** reworded titles with no shared blocking key are missed (accepted, logged
as a recall limitation); threshold tuning is per-dataset and must be re-run when the
platform mix changes; description shingling adds a batch job to the pipeline.

## Reversal cost

Low. `job.cluster_id` is nullable and `is_canonical` defaults to `true`, so an
unclustered database is the initial state. The engine is a batch job that can be
re-run from scratch with a new `dedup_version`. Merges are not destructive because
occurrences are never collapsed. Reverting means clearing `cluster_id` and resetting
`is_canonical`/`merged_into_job_id` for the affected `dedup_version`, not restoring
deleted data.
