# Schema

- **Status:** v2 — revised after adversarial review (see `plans/MUTATIONS.md` M-001).
  Frozen for Slice 1 once approved. Changes after S3 require a migration + mutation entry.
- **Engine:** PostgreSQL 16 (`pg_trgm`, `unaccent`, `pgcrypto`)
- **Authored as:** Postgres-native DDL intent, not ORM models (ADR 0001, reversal cost)
- **Implements:** invariants in `plans/jobindex-blueprint.md` §1

---

## 0. Corrections in v2 (what the review caught)

| Was | Now |
|---|---|
| `job` had a random UUID and no natural key | `job` carries `(origin_source_id, origin_external_id)` with a UNIQUE constraint — re-runs are idempotent by identity, not by luck |
| `UNIQUE (dedup_cluster_id)` made a merged cluster unrepresentable | `job_cluster` table + `is_canonical` + partial unique; losing jobs stay, hidden, with occurrences intact |
| Liveness guard triggered on `job` writes, which never happen on occurrence updates | Deferred constraint trigger on `job_occurrence` — the only table that can actually break the invariant |
| Absence-based closure had no start point | `job.stale_since_at` + `fetch_run.listing_complete`; the predicate is SQL, not prose |
| `fetch_run` written at completion | Written at **claim**, finalized at completion; `abandoned` outcome for crashes |
| Queue uniqueness was vacuous (`gen_random_uuid()` per insert) | Partial unique index on `source_id` for pending/running |
| No platform registry | `platform` table seeded from `docs/platforms.md`; `source.platform` is an FK |

---

## 1. Why these tables

| Table | Answers |
|---|---|
| `platform` | *What platforms exist, which class, which wave, which are excluded?* |
| `source` | *Where do we look, how often, is it healthy, how was it discovered?* |
| `job` | *What is the canonical opening?* (one row per ingested posting, pre-dedup) |
| `job_cluster` | *Which jobs are the same real-world opening?* |
| `job_occurrence` | *Where exactly did we see it, and when?* |
| `fetch_run` | *What did each attempt do?* (backing every coverage claim) |
| `change_event` | *What changed, when, from what value?* |
| `source_run_queue` | *What is scheduled next and who has it?* (ADR 0003) |

`dedup_candidate`, `dedup_rejected_edge` and `job_description_shingle` are created in S9.

---

## 2. Enums

```sql
CREATE TYPE transport_class AS ENUM
  ('api_json', 'embedded_json', 'html', 'headless', 'board_api');

CREATE TYPE platform_class AS ENUM ('C1', 'C2', 'C3', 'C4', 'EX');

CREATE TYPE refresh_tier AS ENUM ('hot', 'warm', 'cool', 'dormant');

CREATE TYPE source_health AS ENUM
  ('healthy', 'degraded', 'open', 'half_open', 'disabled');

CREATE TYPE tenant_status AS ENUM ('candidate', 'validated', 'rejected');

CREATE TYPE job_status AS ENUM ('open', 'stale', 'closed', 'unverified');

CREATE TYPE remote_status AS ENUM ('remote', 'hybrid', 'onsite', 'unknown');

CREATE TYPE employment_type AS ENUM
  ('full_time', 'part_time', 'contract', 'temporary', 'internship',
   'volunteer', 'per_diem', 'unknown');

CREATE TYPE salary_period AS ENUM ('year', 'month', 'week', 'day', 'hour', 'unknown');

CREATE TYPE fetch_outcome AS ENUM ('success', 'partial', 'failure', 'abandoned');

CREATE TYPE error_class AS ENUM
  ('none', 'transient', 'permanent', 'rate_limited', 'blocked', 'not_found', 'parse');

CREATE TYPE verification_status AS ENUM ('ok', 'not_found', 'error');

CREATE TYPE closure_evidence AS ENUM
  ('http_404', 'http_410', 'payload_marker', 'absent_from_full_listing', 'manual');
```

`unknown` is first-class everywhere. Guessing a remote status, employment type or
salary period to fill a column is a data-integrity defect.

---

## 3. `platform` (registry — seeded from `docs/platforms.md`)

Without this table, class, wave and exclusion status live only in Markdown and the
coverage generator cannot be DB-backed (review finding M13).

| Column | Type | Notes |
|---|---|---|
| `key` | `text pk` | `greenhouse`, `workday`, `linkedin` |
| `display_name` | `text not null` | |
| `family` | `text not null` | `ats` / `board` |
| `class` | `platform_class not null` | C1–C4, EX |
| `wave` | `text` | `S3`,`S7`,`S8`,`S10`,`S11`,`S12`,`S13`,`EX` |
| `transport` | `transport_class` | null for EX |
| `auth_kind` | `text not null default 'none'` | `none` / `token` / `partner` |
| `exclusion_reason` | `text` | required when `class = 'EX'` |
| `notes` | `text` | robots/ToS position |

```sql
ALTER TABLE platform ADD CONSTRAINT ex_requires_reason
  CHECK (class <> 'EX' OR exclusion_reason IS NOT NULL);
```

A migration seeds this from `docs/platforms.md`; a test asserts the table and the
document agree row-for-row, so the two cannot drift.

---

## 4. `source` — one row per **tenant**, not per platform

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid pk default gen_random_uuid()` | |
| `platform_key` | `text not null references platform(key)` | |
| `tenant_key` | `text not null` | board slug, company path, or `default` |
| `display_name` | `text` | employer name when known |
| `entry_url` | `text not null` | human-facing careers URL |
| `discovery_url` | `text` | resolved machine endpoint |
| `discovered_via` | `text not null default 'manual'` | `manual` / `sitemap` / `crawl` / `ats-slug` / `registry` |
| `tenant_status` | `tenant_status not null default 'candidate'` | |
| `transport_class` | `transport_class not null` | must equal the connector's declared class |
| `connector` | `text not null` | module implementing the Protocol |
| `refresh_tier` | `refresh_tier not null default 'warm'` | |
| `health` | `source_health not null default 'healthy'` | |
| `consecutive_failures` | `int not null default 0` | |
| `expected_min_jobs` | `int not null default 1` | 0-job runs are anomalous, not "healthy" |
| `last_success_at` | `timestamptz` | |
| `last_attempt_at` | `timestamptz` | |
| `next_due_at` | `timestamptz not null default now()` | **scheduling authority** (sargable) |
| `backoff_until` | `timestamptz` | circuit-breaker authority |
| `robots_note` | `text` | what robots.txt said at review time |
| `tos_note` | `text` | ToS position and reviewer |
| `rate_limit_rps` | `numeric(6,3) not null default 0.5` | |
| `max_concurrency` | `int not null default 1` | |
| `enabled` | `boolean not null default true` | |
| `config` | `jsonb not null default '{}'` | **never contains secrets** (ADR 0005) |
| `created_at` / `updated_at` | `timestamptz` | |

```sql
UNIQUE (platform_key, tenant_key);
CREATE INDEX source_due ON source (next_due_at) WHERE enabled;
ALTER TABLE source ADD CONSTRAINT config_no_secrets
  CHECK (NOT (config ?| ARRAY['token','password','secret','api_key','authorization']));
```

Secrets are resolved from the environment keyed by `source.id` (ADR 0005). A test
asserts no seeded `config` violates the check.

---

## 5. `job` — one row per ingested posting

Identity is `(origin_source_id, origin_external_id)`. A re-run upserts onto the same
row; there is no random-key race (review C1).

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid pk default gen_random_uuid()` | stable, never reused |
| `origin_source_id` | `uuid not null references source(id)` | where it was first ingested |
| `origin_external_id` | `text not null` | platform posting id |
| `title` | `text not null` | canonical title |
| `company` | `text not null` | display form |
| `company_normalized` | `text not null` | trigram matching + blocking key `B2` |
| `company_domain` | `text` | blocking keys `B1`/`B4` |
| `apply_host` | `text` | materialized from the canonical occurrence; blocking key `B4` |
| `description_html` | `text not null default ''` | sanitized |
| `description_text` | `text not null default ''` | plain text, FTS input |
| `content_hash` | `bytea not null` | hash of normalized fields |
| `city_raw` / `city_key` | `text` | key normalized, raw for display |
| `region_raw` / `region_key` | `text` | |
| `country_code` | `text` | `CHECK (country_code ~ '^[A-Z]{2}$')` |
| `location_raw` | `text` | as posted on the canonical occurrence |
| `remote_status` | `remote_status not null default 'unknown'` | |
| `employment_type` | `employment_type not null default 'unknown'` | |
| `salary_min` / `salary_max` | `numeric(12,2)` | normalized, nullable |
| `salary_currency` | `text` | `CHECK (~ '^[A-Z]{3}$')` |
| `salary_period` | `salary_period not null default 'unknown'` | |
| `salary_raw` | `text` | **always preserved** when supplied |
| `salary_is_estimated` | `boolean not null default false` | |
| `posted_at` | `timestamptz` | nullable, source-provided |
| `first_seen_at` | `timestamptz not null default now()` | **immutable** |
| `last_checked_at` | `timestamptz` | advances only on completed fetch |
| `last_changed_at` | `timestamptz` | |
| `status` | `job_status not null default 'unverified'` | |
| `stale_since_at` | `timestamptz` | set on `open → stale`, cleared on reopen |
| `closure_evidence_kind` | `closure_evidence` | required when `closed` |
| `closure_evidence_at` | `timestamptz` | required when `closed` |
| `cluster_id` | `uuid references job_cluster(id)` | null until S9 |
| `is_canonical` | `boolean not null default true` | exactly one per cluster |
| `merged_into_job_id` | `uuid references job(id)` | set on non-canonical members |
| `sort_at` | `timestamptz generated always as (coalesce(posted_at, first_seen_at)) stored` | keyset pagination (review C11) |
| `search_tsv` | `tsvector generated` | ADR 0004 |
| `created_at` / `updated_at` | `timestamptz` | |

```sql
-- identity: idempotent re-ingest
ALTER TABLE job ADD CONSTRAINT job_origin_identity
  UNIQUE (origin_source_id, origin_external_id);

-- salary sanity
ALTER TABLE job ADD CONSTRAINT salary_range_valid
  CHECK (salary_min IS NULL OR salary_max IS NULL OR salary_min <= salary_max);
ALTER TABLE job ADD CONSTRAINT salary_currency_required
  CHECK ((salary_min IS NULL AND salary_max IS NULL) OR salary_currency IS NOT NULL);

-- closure evidence
ALTER TABLE job ADD CONSTRAINT closed_requires_evidence
  CHECK (status <> 'closed' OR (closure_evidence_kind IS NOT NULL
                                AND closure_evidence_at IS NOT NULL));
ALTER TABLE job ADD CONSTRAINT open_forbids_evidence
  CHECK (status <> 'open' OR (closure_evidence_kind IS NULL AND stale_since_at IS NULL));
ALTER TABLE job ADD CONSTRAINT stale_requires_since
  CHECK (status <> 'stale' OR stale_since_at IS NOT NULL);

-- canonical membership
ALTER TABLE job ADD CONSTRAINT merged_requires_cluster
  CHECK (merged_into_job_id IS NULL OR cluster_id IS NOT NULL);
ALTER TABLE job ADD CONSTRAINT canonical_cannot_be_merged
  CHECK (NOT (is_canonical AND merged_into_job_id IS NOT NULL));
```

### The dedup model, stated correctly (review C2)

A cluster is **not** "one job row with a cluster id". It is:

```sql
CREATE TABLE job_cluster (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  canonical_job_id uuid NOT NULL UNIQUE REFERENCES job(id),
  dedup_version text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
```

- Every job keeps its own row and its own occurrences. Occurrences are never moved
  or deleted (invariant 5).
- Exactly one member per cluster is `is_canonical = true`; the rest carry
  `merged_into_job_id`. Enforced by:

```sql
CREATE UNIQUE INDEX job_one_canonical_per_cluster
  ON job (cluster_id) WHERE is_canonical AND cluster_id IS NOT NULL;
```

- `/v1/jobs` filters `is_canonical = true`. A merged duplicate therefore **stops
  appearing** without any row being deleted — which is what the v1 design got wrong.
- `/v1/jobs/{id}` for a canonical job returns occurrences across all cluster members.

#### Authority and write order (verification finding, `plans/MUTATIONS.md` M-002)

Three columns describe the same fact, so the design names one as authoritative:

| Column | Role |
|---|---|
| `job_cluster.canonical_job_id` | **authority** — the cluster's chosen representative |
| `job.is_canonical` | maintained denormalization, so search never joins `job_cluster` |
| `job.merged_into_job_id` | audit pointer for a losing row; never used for filtering |

Without a rule tying them together, `canonical_job_id` can point at a row whose
`is_canonical = false`. The partial unique index does not catch that (it constrains two
*canonical* rows, not zero), and the result is a cluster that exists in the table but
is invisible in `/v1/jobs` — a silently missing job, which is worse than a duplicate.
Enforced with a deferred constraint trigger, fired at commit like the liveness guard:

```sql
CREATE CONSTRAINT TRIGGER cluster_has_canonical
  AFTER INSERT OR UPDATE ON job_cluster
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION assert_cluster_canonical();

-- assert_cluster_canonical() raises unless:
--   EXISTS (SELECT 1 FROM job WHERE id = NEW.canonical_job_id
--                             AND cluster_id = NEW.id AND is_canonical)
```

`job_cluster.canonical_job_id` and `job.cluster_id` reference each other, so neither can
be populated first. The only valid write order inside one transaction is:

1. insert the canonical `job` with `cluster_id = NULL`,
2. insert `job_cluster` with `canonical_job_id = <that job>`,
3. `UPDATE job SET cluster_id = <cluster>` for every member, including the canonical one.

Splitting this across transactions is a bug, not a style choice; the deferred trigger is
what makes the intermediate state legal instead of invisible.

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

## 6. `job_occurrence` — never deleted, never merged

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid pk` | |
| `job_id` | `uuid not null references job(id)` | |
| `source_id` | `uuid not null references source(id)` | |
| `external_id` | `text not null` | platform's posting id |
| `source_url` | `text not null` | listing as seen |
| `apply_urls` | `text[] not null default '{}'` | **all** apply targets (review m2) |
| `apply_url` | `text generated always as (apply_urls[1]) stored` | primary convenience column |
| `title_as_posted` | `text not null` | **verbatim** |
| `location_as_posted` | `text` | **verbatim** |
| `salary_raw_as_posted` | `text` | verbatim |
| `raw_payload_ref` | `text` | fixture path or object-store key |
| `first_seen_at` | `timestamptz not null default now()` | |
| `last_seen_at` | `timestamptz not null` | last time a listing contained it |
| `last_verified_at` | `timestamptz` | last successful detail fetch |
| `last_verification_status` | `verification_status` | enum, not free text |
| `is_live` | `boolean not null default true` | guarded by deferred trigger (§8) |

```sql
UNIQUE (source_id, external_id);
CREATE INDEX occurrence_job_live ON job_occurrence (job_id) WHERE is_live;
CREATE INDEX occurrence_source_stale ON job_occurrence (source_id, is_live, last_seen_at);
CREATE INDEX occurrence_next_check ON job_occurrence (source_id, last_seen_at);
```

`UNIQUE (source_id, external_id)` is the pipeline's idempotency key.

**Derivation rule for `is_live`** (documented, and enforced by trigger in §8):
`is_live = (last_seen_at >= started_at of the most recent listing_complete fetch_run
for this source)`. A detail 404 sets `last_verification_status = 'not_found'` but does
**not** by itself set `is_live = false`.

---

## 7. `fetch_run` — written at claim, finalized at completion

Review C5: writing this row at completion loses the row entirely on a crash, which is
the one case invariant 3 exists to cover.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid pk default gen_random_uuid()` | |
| `source_id` | `uuid not null references source(id)` | |
| `queue_id` | `bigint` | links to `source_run_queue` |
| `started_at` | `timestamptz not null default now()` | |
| `finished_at` | `timestamptz` | null while running |
| `outcome` | `fetch_outcome` | null while running; `abandoned` on crash |
| `http_status` | `int` | |
| `error_class` | `error_class not null default 'none'` | |
| `error_detail` | `text` | truncated |
| `jobs_found` | `int not null default 0` | |
| `jobs_new` | `int not null default 0` | |
| `jobs_updated` | `int not null default 0` | |
| `jobs_stale` | `int not null default 0` | |
| `jobs_closed` | `int not null default 0` | |
| `pages_fetched` / `pages_expected` | `int` | |
| `listing_complete` | `boolean not null default false` | **the only closure gate** |
| `duration_ms` | `int` | |
| `worker_id` | `text` | |

```sql
ALTER TABLE fetch_run ADD CONSTRAINT one_run_per_queue_row UNIQUE (queue_id);
CREATE INDEX fetch_run_source_time ON fetch_run (source_id, started_at DESC);
CREATE INDEX fetch_run_failures ON fetch_run (source_id, started_at DESC)
  WHERE outcome IN ('failure','abandoned');
CREATE INDEX fetch_run_complete_listing ON fetch_run (source_id, started_at DESC)
  WHERE listing_complete;
```

`listing_complete` is set by the connector declaring its own completeness capability
(`Connector.completeness: 'full' | 'windowed' | 'unknown'`) and only becomes true when
every expected page returned. A connector that cannot prove completeness declares
`windowed` and is **never** eligible for absence-based closure.

---

## 8. Liveness enforcement (review C3, M5)

`is_live` is the only field whose change can violate invariant 2, so the guard lives
on the table that changes it, as a deferred constraint trigger.

```sql
CREATE FUNCTION assert_open_jobs_have_live_occurrence() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE bad record;
BEGIN
  SELECT j.id INTO bad
  FROM job j
  WHERE j.status = 'open'
    AND NOT EXISTS (SELECT 1 FROM job_occurrence o
                    WHERE o.job_id = j.id AND o.is_live)
  LIMIT 1;
  IF FOUND THEN
    RAISE EXCEPTION 'job % is open with zero live occurrences', bad.id;
  END IF;
  RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER occurrence_liveness_guard
  AFTER INSERT OR UPDATE OF is_live, job_id ON job_occurrence
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION assert_open_jobs_have_live_occurrence();

CREATE CONSTRAINT TRIGGER job_status_liveness_guard
  AFTER INSERT OR UPDATE OF status ON job
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION assert_open_jobs_have_live_occurrence();
```

Deferred so a transaction may set liveness first and status second in either order.
The reopen path (`closed → open`) is therefore: upsert occurrence with
`is_live = true`, then set status — both in one transaction.

Note the cost: this scans `job` where `status='open'`. The partial index
`job_open_liveness ON job (id) WHERE status = 'open'` plus
`occurrence_job_live` keeps it to an index probe per statement.

---

## 9. `change_event`

| Column | Type | Notes |
|---|---|---|
| `id` | `bigserial pk` | |
| `job_id` | `uuid not null references job(id)` | |
| `occurrence_id` | `uuid references job_occurrence(id)` | null for canonical-level change |
| `fetch_run_id` | `uuid references fetch_run(id)` | provenance |
| `field` | `text not null` | |
| `old_value` / `new_value` | `text` | |
| `detected_at` | `timestamptz not null default now()` | |

```sql
-- idempotent replay (review M8): a re-run cannot duplicate an identical event
ALTER TABLE change_event ADD CONSTRAINT change_event_idempotent
  UNIQUE (occurrence_id, field, new_value, fetch_run_id);
CREATE INDEX change_event_job_time ON change_event (job_id, detected_at DESC);
CREATE INDEX change_event_run ON change_event (fetch_run_id);
```

No `UPDATE`/`DELETE` grants for the application role.

---

## 10. `source_run_queue`

```sql
CREATE TYPE queue_state AS ENUM
  ('pending','running','succeeded','failed','abandoned');

CREATE TABLE source_run_queue (
  id            bigserial PRIMARY KEY,
  source_id     uuid NOT NULL REFERENCES source(id) ON DELETE CASCADE,
  run_id        uuid NOT NULL DEFAULT gen_random_uuid(),
  available_at  timestamptz NOT NULL DEFAULT now(),
  claimed_at    timestamptz,
  claimed_by    text,
  heartbeat_at  timestamptz,
  attempts      int NOT NULL DEFAULT 0,
  state         queue_state NOT NULL DEFAULT 'pending',
  UNIQUE (source_id, run_id)
);

-- the real uniqueness (review C6): one live attempt per source
CREATE UNIQUE INDEX queue_one_live_per_source
  ON source_run_queue (source_id) WHERE state IN ('pending','running');

CREATE INDEX queue_pending ON source_run_queue (available_at) WHERE state = 'pending';
```

The `ON DELETE CASCADE` here only clears ephemeral queue rows. It cannot be used to
erase history: `job_occurrence.source_id` and `fetch_run.source_id` both reference
`source(id)` **without** a cascade, so a `source` row that has ever produced evidence
cannot be deleted at all (review M1). The rollback for a bad source is `enabled = false`,
never `DELETE`.

`fetch_run.queue_id` is deliberately **not** a foreign key: the queue is prunable and
`fetch_run` is the audit record that must outlive it.

### Claim (joins `source`, so `open` sources are genuinely not claimed)

```sql
UPDATE source_run_queue q
SET state = 'running', claimed_at = now(), claimed_by = $1,
    heartbeat_at = now(), attempts = q.attempts + 1
WHERE q.id = (
  SELECT q2.id
  FROM source_run_queue q2
  JOIN source s ON s.id = q2.source_id
  WHERE q2.state = 'pending'
    AND q2.available_at <= now()
    AND s.enabled
    AND s.health <> 'open'
    AND (s.backoff_until IS NULL OR s.backoff_until <= now())
  ORDER BY q2.available_at
  FOR UPDATE OF q2 SKIP LOCKED
  LIMIT 1
)
RETURNING q.*;
```

### Reclaim (specified, not implied)

```sql
WITH stale AS (
  UPDATE source_run_queue
  SET state = 'pending', claimed_by = NULL, claimed_at = NULL
  WHERE state = 'running' AND heartbeat_at < now() - interval '15 minutes'
  RETURNING id, source_id
)
UPDATE fetch_run f
SET outcome = 'abandoned', finished_at = now(),
    error_class = 'transient', error_detail = 'worker heartbeat expired'
FROM stale
WHERE f.queue_id = stale.id AND f.outcome IS NULL;
```

`abandoned`, not `failure` — a crash is not a network fault and must not be counted
against the source's success rate (review C5).

---

## 11. State machine: `job.status`

```
   (first ingest)
        │
        ▼
  ┌─────────────┐  detail OK      ┌────────────┐  absent from successful,
  │ unverified  │────────────────►│    open    │  complete listing
  └─────────────┘                 └─────┬──────┘──────────────┐
                                        │                     ▼
                                        │               ┌───────────┐
                                        │               │   stale   │
        positive evidence only          │               └─────┬─────┘
        (404/410/marker/absence)        ▼                     │ policy window
                                  ┌────────────┐              │ elapsed
                                  │   closed   │◄─────────────┘
                                  └─────┬──────┘
                                        │ occurrence reappears in successful listing
                                        └──────────────► open
```

| Transition | Required evidence | Gate |
|---|---|---|
| `unverified → open` | detail fetch succeeded | `is_live = true` |
| `open → stale` | absent from a **successful**, `listing_complete` fetch | sets `stale_since_at` |
| `stale → closed` | `stale_since_at` older than policy window, **or** 404/410/marker | `closure_evidence_kind` |
| `open → closed` | 404/410 on detail URL, or explicit marker | `closure_evidence_kind` |
| `closed → open` | occurrence reappears in a successful listing | `is_live = true` first |
| `* → open` **never** via | timeout, 429, 5xx, TLS, parse error, `partial` | — |

### The closure predicate, as SQL (review C4)

This is the only query allowed to close jobs. It is a database object, not Python:

```sql
-- candidates: stale past the window, whose source has since completed a full listing
SELECT j.id
FROM job j
JOIN job_occurrence o ON o.job_id = j.id AND o.is_live = false
WHERE j.status = 'stale'
  AND j.stale_since_at < now() - interval '72 hours'
  AND EXISTS (
    SELECT 1 FROM fetch_run f
    WHERE f.source_id = o.source_id
      AND f.listing_complete
      AND f.outcome = 'success'
      AND f.started_at > j.stale_since_at
  );
```

A `partial`, `abandoned`, `failure`, rate-limited or blocked run cannot satisfy
`listing_complete`, so failure-to-close is structural rather than a code convention.

---

## 12. Normalization contract

| Field group | Normalized | Raw | If unparseable |
|---|---|---|---|
| Location | `city_key`, `region_key`, `country_code` | `location_raw` | keys null, raw kept |
| Salary | `salary_min/max/currency/period` | `salary_raw` | normalized null, raw kept |
| Title | `job.title` | `occurrence.title_as_posted` | both always present |
| Description | `description_text` | `description_html` | `''` allowed; never NULL (review m3) |

Employment type and remote status default to `unknown`. Inference is allowed only from
explicit markers or unambiguous phrases, and the inference is recorded in
`change_event` on first write.

---

## 13. S1 baseline table set (explicit, so the migration is not rewritten twice)

**S1 creates:** `platform`, `source`, `job`, `job_cluster`, `job_occurrence`,
`fetch_run`, `change_event`, all enums, all constraints and indexes in §2–§11.

**S2 adds:** no schema change. It adds `jobindex.cli coverage`, which reads these
tables and regenerates the `Verified` column — `verified` is a derived fact from S2
onward, never a hand edit.

**S5 adds:** `source_run_queue` + queue indexes + reclaim.

**S9 adds:** `dedup_candidate`, `dedup_rejected_edge`, `job_description_shingle`,
`job.cluster_id` backfill.

**Not in S1:** queue tables, dedup tables. `job.cluster_id` exists from day one
(nullable) so the canonical filter is stable from the first API call.

---

## 14. Access

- Application role: `SELECT/INSERT/UPDATE`; `DELETE` only on `source_run_queue`.
- No `UPDATE`/`DELETE` on `change_event`.
- `job_occurrence` rows are never deleted. Disabling a source is a flag, not a delete.
  (The S3 rollback instruction is corrected accordingly — see `plans/MUTATIONS.md`.)
