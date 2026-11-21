# Phase 5A Architecture — Vercel + Supabase Cloud-Only Execution

> Status: **Prototype (Phase 5A)**. Not deployed to live resources. This diagram
> documents the minimal serverless execution path proven by the prototype code in
> `src/jobindex/cloud/`, `api/extract.py`, `src/jobindex/api/monitoring.py`,
> `migrations/versions/0004_serverless_execution.py`, `vercel.json`, and
> `supabase/cron.sql`.

```mermaid
flowchart TD
    subgraph Cloud["Vercel + Supabase (runs while local computer is OFF)"]
        direction TB
        Cron[Supabase Cron / pg_cron<br/>every minute]:::cloud
        DB[(Supabase PostgreSQL 16<br/>platform, source, job,<br/>job_occurrence, fetch_run,<br/>serverless_task_queue,<br/>serverless_execution_log)]:::db
        Worker[Vercel Python serverless function<br/>api/extract.handler → FastAPI app<br/>POST /api/extract: claim → process → finalize<br/>retry with backoff → reclaim]:::fn
        Mon[FastAPI monitoring API<br/>GET /api/monitoring/*<br/>summary, sources, platforms<br/>auth: cron secret (fail-closed)]:::fn
        Pool[Supavisor connection pooler]:::pool
    end

    subgraph Local["Developer machine (optional / offline)"]
        CLI[python scripts/verify_cloud.py<br/>or `jobindex cloud verify`<br/>offline simulation]:::local
    end

    Cron -->|1. pg_cron dispatches due<br/>sources into serverless_task_queue| DB
    Cron -.->|2. pg_net.http_post triggers<br/>Vercel worker (POST /api/extract)| Worker
    Worker <-->|3. claim/process via pooled<br/>Postgres (SKIP LOCKED + claim_token fence)| Pool
    Pool <-->|4. SQL| DB
    Mon -.->|5. read-only health queries<br/>(source, fetch_run, queue)| Pool
    Worker -->|6. writes fetch_run + task state +<br/>execution log back to DB| DB

    CLI -.->|7. offline verification<br/>(mocked session, no DB)| Local

    classDef cloud fill:#f9f,stroke:#333,stroke-width:2px;
    classDef db fill:#bbf,stroke:#333,stroke-width:2px;
    classDef fn fill:#bfb,stroke:#333,stroke-width:2px;
    classDef pool fill:#fdd,stroke:#333,stroke-width:2px;
    classDef local fill:#ddd,stroke:#999,stroke-dasharray: 5 5;
```

## How the path works (end to end)

1. **Schedule (cloud)** — Supabase Cron (`pg_cron`, minute resolution) runs two jobs:
   - `jobindex-dispatch-serverless-tasks`: `dispatch_serverless_tasks(25)` enqueues one pending
     task per due `source` (no live attempt already).
   - `jobindex-trigger-vercel-worker`: `pg_net.http_post` to `POST /api/extract` with the cron
     secret bearer header, triggering the Vercel worker.
   Both run in the cloud, independent of the developer's computer. pg_cron is the **single
   scheduler authority**; there is no separate Vercel Cron and no local dependency.

2. **Trigger (cloud)** — pg_cron invokes the Vercel function `api/extract.handler`, which proxies
   the request to the cached FastAPI app (`jobindex.api.app.create_app()`) via ASGITransport.

3. **Auth (fail-closed)** — `POST /api/extract` and all `/api/monitoring/*` endpoints require a
   valid `JOBINDEX_CRON_SECRET` bearer token. If the secret is missing or empty, every protected
   request is rejected (never allowed through unauthenticated).

4. **Claim** — the worker runs `run_dispatch()`, claiming a bounded batch via
   `UPDATE ... FOR UPDATE ... SKIP LOCKED LIMIT N`. Each claim generates a unique `claim_token`;
   the partial unique index `serverless_task_one_live_per_source` guarantees at most one live
   attempt per source, so no duplicate processing.

5. **Process** — for each claimed task: write `fetch_run` (started), refresh the lease heartbeat,
   run the extraction (synthetic fixtures in 5A; real connectors out of scope), finalize the
   `fetch_run` (fenced by `fetch_run` id + `worker_id` + `outcome IS NULL`), update `source`
   health/backoff/next-due, mark the task `succeeded`/`failed` (fenced by `claim_token`), and
   append `serverless_execution_log` events.

6. **Retry / reclaim** — failed tasks return to `pending` with exponential backoff if attempts
   remain; tasks past `max_attempts` are exhausted. Expired leases are reclaimed as `abandoned`
   and their `fetch_run` marked `abandoned`. A stale worker cannot complete another worker's task
   (claim_token fence).

7. **Monitoring (read-only)** — `GET /api/monitoring/summary`, `/api/monitoring/sources`,
   `/api/monitoring/sources/{id}`, `/api/monitoring/platforms` report per-source (website)
   health and active-execution status using `source`, `fetch_run` and `serverless_task_queue`.
   Health status (`source.health`) and active-execution status (running task with valid lease
   heartbeat) are separate dimensions; expired leases are not active scraping, and a failed
   scrape is not proof that jobs closed.

8. **Persist** — all state lives in Supabase PostgreSQL; the function is stateless and short-lived.

## What is intentionally NOT in this diagram

- No platform connectors (`connectors/<platform>.py`) — Phase 5A does not implement new connectors.
- No Playwright / headless browser pool — see feasibility report; HTTP/API extraction is preferred.
- No dashboard — no compatible frontend infrastructure exists yet (planned Jinja UI is S4).
  The monitoring API contract is the deliverable.
- No Redis, Celery, RabbitMQ, AWS, Render, Railway, or a persistent worker fleet — the approved
  stack is Vercel + Supabase only.
- No production database migration or live deployment — requires explicit approval.

## Files

| Layer | File | Role |
|-------|------|------|
| Schema | `migrations/versions/0004_serverless_execution.py` | `serverless_task_queue`, `serverless_execution_log`, `dispatch_serverless_tasks()` |
| Worker logic | `src/jobindex/cloud/queue.py` | SQL claim / complete / reclaim / retry, claim_token fencing |
| Worker logic | `src/jobindex/cloud/worker.py` | `run_dispatch()` orchestration, backoff, lease TTL, refresh heartbeat |
| Monitoring API | `src/jobindex/api/monitoring.py` | `/api/monitoring/*` + `POST /api/extract`, fail-closed auth |
| FastAPI app | `src/jobindex/api/app.py` | includes monitoring router; single app for local + Vercel |
| Synthetic fixtures | `src/jobindex/cloud/fixtures.py` | deterministic test tenants (no network) |
| Verification | `src/jobindex/cloud/verification.py` | reusable offline checks |
| CLI entry | `src/jobindex/cli/main.py` | `jobindex cloud verify` |
| CLI script | `scripts/verify_cloud.py` | standalone verification command |
| Vercel function | `api/extract.py` | `handler` proxies to FastAPI app via ASGITransport |
| Vercel config | `vercel.json` | function timeout, route, cron-secret env |
| Supabase cron | `supabase/cron.sql` | pg_cron dispatch + pg_net worker trigger (single scheduler) |
| Tests | `tests/cloud/test_worker.py` | offline unit tests (10) |
| Tests | `tests/cloud/test_monitoring_auth.py` | offline cron-secret auth tests |
| Tests | `tests/db/test_cloud_queue.py` | PostgreSQL integration tests (concurrency, fencing, retry, recovery) |
| Tests | `tests/db/test_monitoring.py` | PostgreSQL integration tests (monitoring query + aggregation) |
