# ADR 0004 — Search implementation

- **Status:** Accepted
- **Date:** 2026-11-13
- **Affects:** S4 (API + UI), S14 (NFR measurement)

## Context

Target: ~500k active jobs, low-QPS read-heavy API, filters over keyword, company,
location, remote status, employment type, salary floor, posted date, and status.
Requirement: **p95 < 300ms** for filtered queries at that dataset size, with cursor
pagination and stable ordering.

Queries are conventional job search: free-text over title + description, exact-ish
matching on company, and structured filters. There is no need for semantic ranking,
vector search, faceting at scale, or per-tenant index isolation.

## Options considered

| Option | Verdict |
|---|---|
| **Postgres FTS (`tsvector` + GIN) + `pg_trgm`** | Chosen. Native, transactional with the source data, no sync pipeline, meets the latency target with correct indexes at this size. |
| OpenSearch / Elasticsearch | Strong relevance tooling and faceting, but adds a stateful cluster, a sync pipeline, and an entire class of "index drift from source of truth" bugs. Unjustified at 500k rows and low QPS. |
| Typesense / Meilisearch | Much lighter than Elasticsearch and pleasant to operate, but still a second datastore and a sync path. A reasonable *later* upgrade if relevance tuning becomes the bottleneck. |
| SQLite FTS5 | Not applicable: Postgres already required. |

## Decision

**Postgres FTS for keyword search, `pg_trgm` for company/location matching, B-tree
indexes for structured filters, `ts_rank_cd` for ordering within a relevance sort.**

### Indexes

`search_tsv` is **defined once**, as a generated column in `docs/schema.md` §5. This
ADR records why it exists and how it is indexed; it does not redefine the column. Two
definitions of the same generated column would drift.

```sql
-- keyword (column definition lives in docs/schema.md §5)
CREATE INDEX job_search_tsv_gin ON job USING gin (search_tsv);

-- company / location fuzzy
CREATE INDEX job_company_trgm ON job USING gin (company_normalized gin_trgm_ops);
CREATE INDEX job_location_trgm ON job USING gin ((coalesce(city_raw,'') || ' ' || coalesce(region_raw,'')) gin_trgm_ops);

-- filters + keyset ordering
CREATE INDEX job_status_sort ON job (status, sort_at DESC, id DESC);
CREATE INDEX job_sort ON job (sort_at DESC, id DESC) WHERE is_canonical;
CREATE INDEX job_remote_type ON job (remote_status, employment_type) WHERE status = 'open';
CREATE INDEX job_salary_floor ON job (salary_min) WHERE salary_min IS NOT NULL;
```

`sort_at` is `GENERATED ALWAYS AS (coalesce(posted_at, first_seen_at)) STORED` and is
therefore `NOT NULL` — that is the whole point of it. Ordering on `posted_at` directly
is `NULL`-unsafe: `WHERE (posted_at, id) < ($ts, $id)` silently drops every row with a
null `posted_at`, and a `NULLS LAST` sort cannot be used as a keyset boundary.

### Query construction

- Keyword: `websearch_to_tsquery('english', $q)` — tolerates user punctuation and
  quoted phrases, unlike `to_tsquery`.
- Company: exact match on `company_normalized` first; fall back to trigram similarity
  with a floor (`similarity(...) >= 0.6`) and a bounded result set.
- Relevance ordering: `ts_rank_cd(search_tsv, query)`; default ordering for the UI is
  `sort_at DESC, id DESC` because recency beats relevance in job search.
- Every filter is applied as a conjunctive `WHERE`; the primary query uses
  `EXPLAIN (ANALYZE, BUFFERS)` in CI to assert the plan uses the intended index and
  performs no sequential scan on `job` at **500k** rows.

### Pagination

Opaque cursor encoding `(sort_at, id)`; the page predicate is
`WHERE (sort_at, id) < ($sort_at, $id)` with `ORDER BY sort_at DESC, id DESC`.
This is total and `NULL`-safe because `sort_at` is `NOT NULL` by construction, and
`id` breaks ties. Offset pagination is rejected — it degrades linearly and produces
duplicates and skips as data shifts during paging.

`/v1/jobs` additionally filters `is_canonical = true`, so a merged duplicate stops
appearing without its row being deleted.

### Degradation path

If the query text is empty, skip FTS entirely and serve the structured filters off
`job_status_sort`. The UI's default view must never pay for a tsquery it does not need.

## Measured triggers for revisiting

This ADR is revisited only on evidence. Any one of these justifies a new ADR:

1. p95 search latency > 300ms at 500k rows after index review, sustained over 7 days.
2. Relevance complaints that cannot be fixed by weighted `tsvector` fields or
   `ts_rank_cd` tuning (e.g. the need for synonym sets, per-field boosting beyond three
   weights, or typo-tolerant whole-document matching).
3. Need for faceted counts (company/location histograms) in the results view — FTS
   handles this poorly at scale without pre-aggregation.
4. Dataset growth beyond ~2M active jobs.

If triggered, the migration target is **Typesense** (lightest operational footprint
that solves relevance + facets), not Elasticsearch.

## Consequences

**Positive:** no index-sync pipeline; search results are always consistent with the
source tables in the same transaction; `EXPLAIN`-verifiable performance; one backup
strategy.

**Negative:** relevance tuning is coarser than a dedicated engine; no typo tolerance
on free text (trigram helps company/location only); faceted counts are expensive and
are therefore out of scope for v1.

## Reversal cost

Moderate but contained: add an indexing pipeline and a query adapter; the API contract
(`/v1/jobs`, `/v1/jobs/{id}`) does not change, so consumers are unaffected. This is why
the API contract is specified independently in `docs/openapi.yaml` rather than being
derived from the search engine.
