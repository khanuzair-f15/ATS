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
  heartbeat_at      timestamptz null -- liveness of the claim; drives reclaim
  attempts          int not null default 0
  state             enum('pending','running','succeeded','failed','abandoned')
  unique (source_id, run_id)
  unique (source_id) where state in ('pending','running')   -- one live attempt
  index (available_at) where state = 'pending'
```

The partial unique index is the real scheduling invariant. `UNIQUE (source_id, run_id)`
is vacuous on its own because `run_id` defaults to a fresh UUID on every insert.

### Enqueue (one tick, one transaction)

`source.next_due_at` is the scheduling authority; the queue holds only **live
attempts**. Three statements, all idempotent, run in one transaction by the scheduler
tick, **in this order**:

```sql
-- 1. an open breaker whose backoff has expired gets one probe
UPDATE source SET health = 'half_open'
WHERE enabled AND health = 'open' AND backoff_until <= now();

-- 2. give every due source exactly one pending attempt
INSERT INTO source_run_queue (source_id, available_at)
SELECT s.id, now()
FROM source s
WHERE s.enabled
  AND s.health <> 'open'
  AND s.next_due_at <= now()
  AND (s.backoff_until IS NULL OR s.backoff_until <= now())
  AND NOT EXISTS (
    SELECT 1 FROM source_run_queue q
    WHERE q.source_id = s.id AND q.state IN ('pending','running')
  );

UPDATE source
SET next_due_at = now() + CASE refresh_tier
    WHEN 'hot'  THEN interval '6 hours'
    WHEN 'warm' THEN interval '12 hours'
    WHEN 'cool' THEN interval '24 hours'
    ELSE interval '72 hours' END
WHERE enabled AND health <> 'open' AND next_due_at <= now();
```

Running these out of order, or outside one transaction, produces either a duplicate
attempt or a source that is never scheduled again. The partial unique index makes the
insert safe under two concurrent scheduler ticks.

The third statement deliberately excludes `health = 'open'`: an open source's
`next_due_at` is left in the past so that when statement 1 promotes it to `half_open`,
it is immediately due and the probe runs on the next tick rather than waiting up to 72
hours for its tier interval to elapse.

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

### Complete

In the **same transaction** as the data writes:

1. upsert occurrences on `(source_id, external_id)`,
2. insert `change_event` rows for detected field drift,
3. finalize the `fetch_run` row created at claim (`finished_at`, `outcome`, counts),
4. update `source` health (`last_success_at`, `last_attempt_at`,
   `consecutive_failures`, `backoff_until`) and scheduling:
   - success/half-open success → `next_due_at = now() + tier interval`,
     `backoff_until = NULL`
   - qualifying failure → `backoff_until = now() + retry_delay(n)` and
     `next_due_at = least(next_due_at, backoff_until)` so a degraded source retries
     sooner than its tier interval rather than waiting 12–72 hours
5. set queue row `succeeded` / `failed`.

A long connector run updates `heartbeat_at` every 60s on its queue row. If the
transaction rolls back, the queue row stays `running`; the reclaimer below is what
makes delivery at-least-once and the writes idempotent.

### Reclaim and the `abandoned` path

`fetch_run` is **created at claim**, not at completion (`docs/schema.md` §7). A crash
therefore leaves a visible `NULL`-outcome run instead of no row at all — invariant 3
("no silent failures") otherwise has a hole exactly where it matters most.

```sql
WITH stale AS (
  UPDATE source_run_queue
  SET state = CASE WHEN attempts >= 3 THEN 'abandoned' ELSE 'pending' END,
      claimed_by = NULL, claimed_at = NULL
  WHERE state = 'running'
    AND heartbeat_at < now() - interval '15 minutes'
  RETURNING id, source_id, state
)
UPDATE fetch_run f
SET outcome = 'abandoned', finished_at = now(),
    error_class = 'transient', error_detail = 'worker heartbeat expired'
FROM stale
WHERE f.queue_id = stale.id AND f.outcome IS NULL;
```

- `abandoned` is **not** `failure`. A dead worker is not a fault of the source and must
  not count against its success rate or trip its circuit breaker.
- After 3 expired claims the queue row is marked `abandoned` and is not re-enqueued
  automatically; the next scheduled attempt for that source is a fresh row. A source
  that reliably kills workers must surface as an operator incident, not as an infinite
  retry loop.
- The reclaimer is idempotent: the final `UPDATE ... WHERE f.outcome IS NULL` matches
  nothing on a second pass.

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

States: `healthy → degraded → open → half_open → healthy`, with `degraded → healthy`
on recovery.

**Counter semantics (`source.consecutive_failures`).** The counter is driven by
`fetch_run.outcome` + `fetch_run.error_class`, one row per attempt:

| Run result | Counter | Health effect |
|---|---|---|
| `success` | reset to 0 | `degraded → healthy`, `half_open → healthy` |
| `partial` | unchanged | no health change (evidence is incomplete, not absent) |
| `failure`, `error_class ∈ {transient, rate_limited, parse}` | +1 | `healthy → degraded` at 1–2; `→ open` at 3 |
| `failure`, `error_class = permanent` or source-root `not_found` | +1 **and** `→ open` immediately | a 404 on the board itself is not transient |
| `failure`, `error_class = blocked` | +1 | first `blocked`: backoff, stay `degraded`; **second consecutive** `blocked`: `→ open` |
| `abandoned` | unchanged | none — worker crash, not a source fault |

Two `blocked` observations are required before `open` because a single WAF response is
often a bad minute, and opening the breaker on it hides a healthy source for hours.
`blocked` still backs off immediately after the first observation.

- `degraded`: 1–2 consecutive qualifying failures. Still scheduled at tier interval.
- `open`: scheduling stops; `backoff_until = now() + min(base * 2^(n-3), max_backoff)`
  with jitter (base 30m, max 24h).
- `half_open`: one probe run after backoff. Success → `healthy`, counter reset.
  Failure → `open` with a larger backoff.
- A source in `open` is **still visible** in `/v1/sources` and in the coverage report
  as degraded. It is never silently dropped.

**Critical rule:** entering `open` never closes jobs. It only stops scheduling. Closure
is governed by the S6 invariant suite and the closure predicate in `docs/schema.md`
§11, and requires positive evidence. A source that fails for a week leaves its jobs
`open` with a rising staleness age and an explicit `stale` signal in the API — which is
honest, whereas closing them would be a lie.

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
