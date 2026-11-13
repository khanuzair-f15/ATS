# ADR 0003 — Scheduling and queue

- **Status:** Accepted
- **Date:** 2026-11-13
- **Affects:** S1 (compose), S5 (scheduler), S6 (closure semantics), S14 (NFRs)

## Context

The system must re-check 78 platforms on tiered schedules, in one region, on one
Postgres, with one worker fleet. Requirements that actually constrain the design:

1. **At-least-once delivery** with **idempotent writes** — a duplicate run must not
   duplicate jobs.
2. **Per-source isolation** — one broken source must never stall the fleet.
3. **No silent failures** — every attempt writes a `fetch_run` row.
4. **Failure ≠ closure** — the queue's retry semantics must not be interpretable as
   evidence of a closed listing.
5. **Backoff and circuit breaking** per source, not global.
6. Survive a worker restart mid-run without losing or double-counting a source.

## Options considered

| Option | Verdict |
|---|---|
| Celery + Redis | Mature, but adds Redis as a second stateful dependency and Celery's visibility timeouts are a known source of duplicate work. Operationally heavier than the workload justifies. |
| RQ + Redis | Simpler than Celery, still a second datastore. Visibility timeout semantics are weaker. |
| Dramatiq + Redis | Good design, same Redis cost. |
| ARQ + Redis | asyncio-native, small, but Redis again and a thin maintenance story. |
| **Postgres `FOR UPDATE SKIP LOCKED` (chosen)** | Zero new infrastructure, transactional claim/complete, trivially auditable with SQL, naturally idempotent when paired with unique keys. |
| `procrastinate` (library over the above) | Same model with batteries. Acceptable implementation detail; the ADR's decision is the *mechanism*, not the library. |

The brief explicitly constrains the system to "one Postgres". Adding Redis would
contradict that for no measured benefit at 1–3M requests/day.

## Decision

**A Postgres-backed work queue using `FOR UPDATE SKIP LOCKED`, with per-source
uniqueness, transactional claim/complete, and `fetch_run` as the single audit record.**

### Queue table shape

```
source_run_queue
  id                bigserial pk
  source_id         fk -> source
  run_id            uuid            -- one logical refresh attempt
  available_at      timestamptz     -- scheduling + backoff
  claimed_at        timestamptz null
  claimed_by        text null       -- worker id
  attempts          int not null default 0
  state             enum('pending','running','succeeded','failed','abandoned')
  unique (source_id, run_id)
  index (state, available_at) where state = 'pending'
```

### Claim

```sql
UPDATE source_run_queue q
SET state = 'running', claimed_at = now(), claimed_by = $1, attempts = attempts + 1
WHERE q.id = (
  SELECT id FROM source_run_queue
  WHERE state = 'pending' AND available_at <= now()
  ORDER BY available_at
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
RETURNING *;
```

### Complete

In the **same transaction** as the data writes:

1. upsert occurrences on `(source_id, external_id)`,
2. insert `change_event` rows for detected field drift,
3. insert exactly one `fetch_run` row,
4. update `source` health (`last_success_at`, `consecutive_failures`, `backoff_until`),
5. set queue row `succeeded` / `failed`.

If the transaction rolls back, the queue row stays `running`. A reclaimer marks
`running` rows older than `claim_timeout` as `pending` with `attempts` intact. This is
what makes delivery at-least-once and writes idempotent: replaying a run re-upserts
the same rows and produces the same end state.

### Refresh tiers

Tier lives on `source.refresh_tier`, tunable by a config row without redeploy:

| Tier | Interval | Typical members |
|---|---|---|
| `hot` | 6h | high-volume ATS tenants, major boards |
| `warm` | 12h | default |
| `cool` | 24h | low-churn enterprise career sites |
| `dormant` | 72h | sources with < N jobs found over 14 days |

Tier promotion happens on observed churn (jobs changed per run), not on a static list.
`hot` sources are staggered across their window so a full-fleet refresh never
converges on one moment.

### Failure handling and circuit breaker

States: `healthy → degraded → open → half_open → healthy`.

- `degraded`: 1–2 consecutive failures. Still scheduled at tier interval.
- `open`: 3+ consecutive failures, or a `blocked` error class. Scheduling stops;
  `backoff_until = now() + min(base * 2^(n-3), max_backoff)` with jitter
  (base 30m, max 24h).
- `half_open`: one probe run after backoff. Success → `healthy`; failure → `open`
  with a larger backoff.
- A source in `open` is **still visible** in `/v1/sources` and in the coverage report
  as degraded. It is never silently dropped.

**Critical rule:** entering `open` never closes jobs. It only stops scheduling. Closure
is governed by ADR-0002-era rules in S6 and requires positive evidence. A source that
fails for a week leaves its jobs `open` with a rising `last_checked_at` age and an
explicit `stale` signal in the API — which is honest, whereas closing them would be a lie.

## Consequences

**Positive:** one datastore; claim and completion are transactional; the queue is
queryable with SQL during incidents; per-source isolation is explicit; no message
broker to operate or secure.

**Negative:** polling has slightly higher latency than a broker push (irrelevant at
6–24h cadence); high-frequency scheduling would need partitioning (not needed here);
`SKIP LOCKED` plus long transactions requires discipline — every connector call must
happen **outside** the claim transaction, with writes in a short final transaction.

## Reversal cost

Low–moderate. The queue is an internal detail behind a `Queue` interface; swapping to
Celery or SQS means reimplementing claim/complete/complete-with-writes. The `fetch_run`
audit table and the circuit-breaker state on `source` are storage-agnostic and survive
the change.
