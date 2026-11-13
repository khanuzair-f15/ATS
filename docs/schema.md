# Schema

- **Status:** Frozen for Slice 1. Changes after S7 require a migration + ADR note.
- **Engine:** PostgreSQL 16 (`pg_trgm`, `unaccent`, `pgcrypto`)
- **Authored as:** Postgres-native DDL intent, not ORM models (see ADR 0001, reversal cost)
- **Implements:** invariants in `plans/jobindex-blueprint.md` §1

---

## 1. Why six tables

| Table | Answers |
|---|---|
| `source` | *Where do we look, how often, and is it healthy?* |
| `job` | *What is the canonical opening?* |
| `job_occurrence` | *Where exactly did we see it, and when?* |
| `fetch_run` | *What did each attempt do?* (the audit trail behind every coverage claim) |
| `change_event` | *What changed, when, and from what value?* |
| `source_run_queue` | *What is scheduled next and who has it?* (ADR 0003) |

Two supporting tables are created in S9: `dedup_candidate` (review band, ADR 0002) and
`job_description_shingle` (MinHash inputs). They are deliberately **not** in the Slice 1
migration so the core shape stays small.

---

## 2. Enums

```sql
CREATE TYPE transport_class AS ENUM
  ('api_json', 'embedded_json', 'html', 'headless', 'board_api');

CREATE TYPE refresh_tier AS ENUM ('hot', 'warm', 'cool', 'dormant');

CREATE TYPE source_health AS ENUM
  ('healthy', 'degraded', 'open', 'half_open', 'disabled');

CREATE TYPE job_status AS ENUM ('open', 'stale', 'closed', 'unverified');

CREATE TYPE remote_status AS ENUM ('remote', 'hybrid', 'onsite', 'unknown');

CREATE TYPE employment_type AS ENUM
  ('full_time', 'part_time', 'contract', 'temporary', 'internship',
   'volunteer', 'per_diem', 'unknown');

CREATE TYPE salary_period AS ENUM ('year', 'month', 'week', 'day', 'hour', 'unknown');

CREATE TYPE fetch_outcome AS ENUM ('success', 'partial', 'failure');

CREATE TYPE error_class AS ENUM
  ('none', 'transient', 'permanent', 'rate_limited', 'blocked', 'not_found', 'parse');

CREATE TYPE closure_evidence AS ENUM
  ('http_404', 'http_410', 'payload_marker', 'absent_from_full_listing', 'manual');
```

`unknown` is a first-class value everywhere. Guessing a remote status, employment type
or salary period to fill a column is a data-integrity defect, not a convenience.

---

## 3. `source`

One row per **tenant**, not per platform. `greenhouse` is a platform;
`greenhouse/acme-corp` is a source.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid pk default gen_random_uuid()` | |
| `platform` | `text not null` | e.g. `greenhouse`, `workday` — matches `docs/platforms.md` |
| `tenant_key` | `text not null` | board slug, company path, or `default` |
| `display_name` | `text` | employer name when known |
| `entry_url` | `text not null` | the human-facing careers URL |
| `discovery_url` | `text` | resolved machine endpoint, if different |
| `transport_class` | `transport_class not null` | |
| `connector` | `text not null` | module name implementing the Protocol |
| `refresh_tier` | `refresh_tier not null default 'warm'` | |
| `health` | `source_health not null default 'healthy'` | |
| `consecutive_failures` | `int not null default 0` | |
| `last_success_at` | `timestamptz` | |
| `last_attempt_at` | `timestamptz` | |
| `backoff_until` | `timestamptz` | |
| `robots_note` | `text` | what robots.txt said at review time |
| `tos_note` | `text` | ToS position and reviewer |
| `rate_limit_rps` | `numeric(6,3) not null default 0.5` | |
| `max_concurrency` | `int not null default 1` | |
| `enabled` | `boolean not null default true` | |
| `config` | `jsonb not null default '{}'` | per-tenant path/pagination hints |
| `created_at` / `updated_at` | `timestamptz` | |

```sql
UNIQUE (platform, tenant_key);
CREATE INDEX source_due ON source (refresh_tier, last_attempt_at) WHERE enabled;
```

**Rule:** `transport_class` on the source must match the connector's declared class.
A mismatch is a configuration error surfaced in `/v1/sources`.

---

## 4. `job`

The canonical record. One row per real-world opening after dedup.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid pk` | stable, never reused |
| `dedup_cluster_id` | `uuid` | nullable until S9 |
| `title` | `text not null` | canonical title |
| `company` | `text not null` | display form |
| `company_normalized` | `text not null` | for trigram matching |
| `company_domain` | `text` | blocking key `B1`/`B4` |
| `description_html` | `text` | sanitized |
| `description_text` | `text` | plain text, FTS input |
| `content_hash` | `bytea not null` | hash of normalized fields |
| `city_raw` / `city_key` | `text` | key is normalized, raw is display |
| `region_raw` / `region_key` | `text` | |
| `country_code` | `char(2)` | ISO-3166-1 alpha-2, nullable when unknown |
| `location_raw` | `text` | exactly as posted on the canonical occurrence |
| `remote_status` | `remote_status not null default 'unknown'` | |
| `employment_type` | `employment_type not null default 'unknown'` | |
| `salary_min` / `salary_max` | `numeric(12,2)` | normalized, nullable |
| `salary_currency` | `char(3)` | ISO-4217 |
| `salary_period` | `salary_period not null default 'unknown'` | |
| `salary_raw` | `text` | **always preserved** when the source gave one |
| `salary_is_estimated` | `boolean not null default false` | |
| `posted_at` | `timestamptz` | source-provided or derived, nullable |
| `first_seen_at` | `timestamptz not null default now()` | **immutable** |
| `last_checked_at` | `timestamptz` | advances only on completed fetch |
| `last_changed_at` | `timestamptz` | |
| `status` | `job_status not null default 'unverified'` | |
| `closure_evidence_kind` | `closure_evidence` | required when `closed` |
| `closure_evidence_at` | `timestamptz` | required when `closed` |
| `search_tsv` | `tsvector` generated | ADR 0004 |
| `created_at` / `updated_at` | `timestamptz` | |

```sql
ALTER TABLE job ADD CONSTRAINT salary_range_valid
  CHECK (salary_min IS NULL OR salary_max IS NULL OR salary_min <= salary_max);

ALTER TABLE job ADD CONSTRAINT salary_currency_required
  CHECK (salary_min IS NULL AND salary_max IS NULL OR salary_currency IS NOT NULL);

ALTER TABLE job ADD CONSTRAINT closed_requires_evidence
  CHECK (status <> 'closed' OR (closure_evidence_kind IS NOT NULL
                                AND closure_evidence_at IS NOT NULL));

ALTER TABLE job ADD CONSTRAINT open_forbids_evidence
  CHECK (status <> 'open' OR closure_evidence_kind IS NULL);

-- one canonical job per cluster
CREATE UNIQUE INDEX job_one_canonical_per_cluster
  ON job (dedup_cluster_id) WHERE dedup_cluster_id IS NOT NULL;
```

### Immutability of `first_seen_at`

```sql
CREATE FUNCTION job_first_seen_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.first_seen_at <> OLD.first_seen_at THEN
    RAISE EXCEPTION 'job.first_seen_at is immutable';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER job_first_seen_guard BEFORE UPDATE ON job
  FOR EACH ROW EXECUTE FUNCTION job_first_seen_immutable();
```

---

## 5. `job_occurrence`

One row per `(job, source)`. Never deleted, never merged. This is what makes
"also seen on" and provenance possible.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid pk` | |
| `job_id` | `uuid not null references job(id)` | |
| `source_id` | `uuid not null references source(id)` | |
| `external_id` | `text not null` | platform's posting id |
| `source_url` | `text not null` | the listing as seen |
| `apply_url` | `text` | the application target, when distinct |
| `title_as_posted` | `text not null` | **verbatim**, never normalized |
| `location_as_posted` | `text` | **verbatim** |
| `salary_raw_as_posted` | `text` | verbatim |
| `raw_payload_ref` | `text` | fixture path or object-store key |
| `first_seen_at` | `timestamptz not null default now()` | |
| `last_seen_at` | `timestamptz not null` | last time the listing contained it |
| `last_verified_at` | `timestamptz` | last successful detail fetch |
| `last_verification_status` | `text` | `ok` / `not_found` / `error` |
| `is_live` | `boolean not null default true` | current presence flag |
| `created_at` / `updated_at` | `timestamptz` | |

```sql
UNIQUE (source_id, external_id);
CREATE INDEX occurrence_next_check ON job_occurrence (source_id, last_seen_at);
CREATE INDEX occurrence_job ON job_occurrence (job_id);
```

`UNIQUE (source_id, external_id)` is the idempotency key for the whole pipeline.
Replaying a run upserts onto the same rows.

---

## 6. `fetch_run`

The audit trail. **Every attempt writes exactly one row**, including hard crashes
(inserted on claim, finalized on completion; a crashed run is finalized by the
reclaimer as `failure` with `error_class = 'transient'`).

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid pk` | |
| `source_id` | `uuid not null references source(id)` | |
| `queue_id` | `bigint` | links to `source_run_queue` |
| `started_at` | `timestamptz not null` | |
| `finished_at` | `timestamptz` | null while running |
| `outcome` | `fetch_outcome` | null while running |
| `http_status` | `int` | |
| `error_class` | `error_class not null default 'none'` | |
| `error_detail` | `text` | truncated stack / status text |
| `jobs_found` | `int not null default 0` | |
| `jobs_new` | `int not null default 0` | |
| `jobs_updated` | `int not null default 0` | |
| `jobs_stale` | `int not null default 0` | |
| `jobs_closed` | `int not null default 0` | |
| `pages_fetched` / `pages_expected` | `int` | drives `partial` |
| `duration_ms` | `int` | |
| `worker_id` | `text` | |

```sql
CREATE INDEX fetch_run_source_time ON fetch_run (source_id, started_at DESC);
CREATE INDEX fetch_run_failures ON fetch_run (source_id, started_at DESC)
  WHERE outcome = 'failure';
```

The coverage report in S14 is a query over this table. **No "working" claim may exist
without a `success` row here.**

---

## 7. `change_event`

Append-only. Enables "what changed and when" without overwriting history.

| Column | Type | Notes |
|---|---|---|
| `id` | `bigserial pk` | |
| `job_id` | `uuid not null references job(id)` | |
| `occurrence_id` | `uuid references job_occurrence(id)` | null for canonical-level change |
| `fetch_run_id` | `uuid references fetch_run(id)` | provenance of the detection |
| `field` | `text not null` | e.g. `salary_max`, `status`, `title` |
| `old_value` | `text` | |
| `new_value` | `text` | |
| `detected_at` | `timestamptz not null default now()` | |

```sql
CREATE INDEX change_event_job_time ON change_event (job_id, detected_at DESC);
```

No `UPDATE` or `DELETE` grants on this table for the application role. Corrections are
new events.

---

## 8. `source_run_queue`

Defined in ADR 0003. Created in S5, not S1, so Slice 1 needs no queue.

```sql
CREATE TYPE queue_state AS ENUM ('pending','running','succeeded','failed','abandoned');

CREATE TABLE source_run_queue (
  id            bigserial PRIMARY KEY,
  source_id     uuid NOT NULL REFERENCES source(id) ON DELETE CASCADE,
  run_id        uuid NOT NULL DEFAULT gen_random_uuid(),
  available_at  timestamptz NOT NULL DEFAULT now(),
  claimed_at    timestamptz,
  claimed_by    text,
  attempts      int NOT NULL DEFAULT 0,
  state         queue_state NOT NULL DEFAULT 'pending',
  UNIQUE (source_id, run_id)
);
CREATE INDEX queue_pending ON source_run_queue (available_at)
  WHERE state = 'pending';
```

---

## 9. State machine: `job.status`

```
                  ┌──────────────────────────────────────────┐
                  │                                          │
   (first ingest) │                                          │
        ┌─────────▼─────────┐   successful full listing      │
        │    unverified     │───────────────────────────────► │
        └─────────┬─────────┘                                │
                  │ detail fetch OK                           │
                  ▼                                           │
        ┌───────────────────┐  absent from successful        │
        │       open        │  full listing ──────────► stale│
        └─────────┬─────────┘                                │
                  │                                          │
   positive evidence only                                    │
   (404/410/marker/absence)                                  │
                  ▼                                          │
        ┌───────────────────┐   occurrence reappears          │
        │      closed       │──────────────────────────────────┘
        └───────────────────┘        (reopen)
```

| Transition | Allowed evidence |
|---|---|
| `unverified → open` | detail fetch succeeded |
| `unverified → stale` | listed but detail never confirmed |
| `open → stale` | absent from a **successful, complete** listing fetch |
| `stale → closed` | `absent_from_full_listing` after the policy window, or 404/410/marker |
| `open → closed` | 404/410 on detail URL, or explicit closed marker in payload |
| `closed → open` | occurrence reappears in a successful listing |
| `* → open` never via | timeout, 429, 5xx, TLS error, parse error, `partial` listing |

### Enforced in three places

1. **`CHECK` constraints** — `closed` requires an evidence kind; `open` forbids one.
2. **Trigger `occurrence_liveness_guard`** — before setting a job `open`, asserts at
   least one `job_occurrence` with `is_live = true` exists. This is the DB-level
   enforcement of "zero live occurrences cannot be open".
3. **Invariant tests (S6)** — table-driven, one named test per transition above,
   including the negative cases (429/timeout/5xx/partial must not close).

---

## 10. Normalization contract

Stored twice on purpose: normalized fields for query/dedup, raw fields for display and
audit.

| Field group | Normalized | Raw | If unparseable |
|---|---|---|---|
| Location | `city_key`, `region_key`, `country_code` | `location_raw` | keys null, raw kept |
| Salary | `salary_min/max/currency/period` | `salary_raw` | normalized null, raw kept, `is_estimated=false` |
| Title | `job.title` | `occurrence.title_as_posted` | both always present |
| Description | `description_text` | `description_html` | sanitized, never empty-string-masquerading-as-null |

**Employment type and remote status** default to `unknown`. Inference is allowed only
from explicit payload markers or unambiguous phrases, and the inference source is
recorded in `change_event` on first write.

---

## 11. Migrations and access

- Alembic, forward-only in production. Destructive changes use expand → backfill →
  contract across two releases.
- Application role: `SELECT/INSERT/UPDATE` on all tables, `DELETE` on
  `source_run_queue` only. No `UPDATE/DELETE` on `change_event`.
- `job_occurrence` rows are never deleted, even when a source is disabled.

---

## 12. Seed data for Slice 1

`S1` seeds nothing. `S3` inserts exactly one `source` row and its occurrences from
recorded fixtures, which is what makes the end-to-end test deterministic.
