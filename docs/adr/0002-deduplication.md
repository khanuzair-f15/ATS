# ADR 0002 — Deduplication strategy

- **Status:** Proposed — thresholds are **provisional** and must be replaced by measured
  values in S9 before this ADR is marked Accepted
- **Date:** 2026-11-13
- **Affects:** S3 (schema support), S9 (engine + evaluation), S14 (coverage)

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
| `B2` | `(company_key, city_key)` | same employer + city, different title phrasing |
| `B3` | `(apply_host, external_id)` | literally the same req re-ingested |
| `B4` | `(company_domain, apply_host)` | same req cross-posted to a board |

`title_stem` = normalized title with department/track qualifiers kept and formatting
removed. Keys with null components are skipped, not coalesced — a null city must not
block every remote job together.

Expected candidate volume at 500k jobs: ~1–3% of all pairs, i.e. 2.5–7.5M comparisons,
which is a batch job measured in minutes on one process pool.

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

- Different `apply_host` **and** different `external_id` **and** different `city`
  ⇒ **no merge** unless description Jaccard ≥ `HARD_DESC_SIM`.
- Any two jobs whose `external_id` are both non-null and different **and** which come
  from the same `source` ⇒ **no merge** (same board, different reqs).
- Jobs with `remote_status = remote` and different `country_code` ⇒ no merge unless
  description similarity is high (a "Senior Engineer (Remote)" posting can legitimately
  be region-locked twice).

### 5. Thresholds

Provisional starting grid, to be replaced in S9 by a sweep on the labeled set:

| Band | Score | Action |
|---|---|---|
| auto-merge | ≥ 0.88 | merge |
| review | 0.72 – 0.88 | merge only if hard constraints pass **and** description sim ≥ 0.55; otherwise hold apart and log to `dedup_candidate_queue` |
| no-merge | < 0.72 | separate |

The review band exists so the operator can see near-misses. It is not auto-merged.

### 6. Clustering

Union-find over accepted pairs within a blocking pass. Transitive closure is applied
but **capped**: if a cluster would exceed `MAX_CLUSTER_SIZE` (default 6 occurrences),
the weakest edge is dropped and the event logged. This prevents a single bad pair from
fusing an entire department into one job.

`job.dedup_cluster_id` points at the canonical cluster. `job_occurrence` rows are never
merged or deleted — the occurrence table is the audit trail and the source of
"also seen on".

Canonical-field selection inside a cluster: prefer the occurrence with the most
complete record (description length, salary present, apply URL reachable), tie-broken
by earliest `first_seen_at`. The chosen record's `content_hash` drives change events.

## Evaluation protocol (S9, mandatory)

- **~200 hand-labeled pairs**, drawn from real captured data after Wave A, not invented:
  - ~100 true duplicates (cross-source same job, reworded titles, reposted-with-edits)
  - ~100 hard negatives (same title/different city, same title/different team,
    same company/different req id, same board/different posting)
- Labels stored in `tests/dedup/labeled_pairs.jsonl` with the reason for each label.
- Report: precision, recall, F1, the full threshold sweep, and a confusion breakdown
  by feature (which features cause false merges).
- Report **per-blocking-key** recall: if `B2` contributes nothing, drop it.
- A named list of known failure modes with an example each.

## Consequences

**Positive:** explainable merges, sub-quadratic cost, no new infrastructure, thresholds
change without a schema migration, every source URL preserved.

**Negative:** reworded titles with no shared blocking key are missed (accepted, logged
as a recall limitation); threshold tuning is per-dataset and must be re-run when the
platform mix changes; description shingling adds a batch job to the pipeline.

## Reversal cost

Low. `dedup_cluster_id` is nullable; the engine is a batch job that can be re-run from
scratch with a new `dedup_version`. Merges are not destructive because occurrences are
never collapsed. Reverting means setting the column to null, not restoring data.
