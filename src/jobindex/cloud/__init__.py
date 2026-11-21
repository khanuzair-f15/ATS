"""Phase 5A cloud-only execution prototype: Vercel serverless + Supabase Postgres.

This package implements the minimal serverless execution path:

1. Supabase PostgreSQL stores pending extraction tasks and execution state
   (`serverless_task_queue`, `serverless_execution_log`; migration 0004).
2. Supabase Cron (pg_cron) dispatches due sources into the queue
   (`supabase/cron.sql`) so indexing continues while the local computer is off.
3. Vercel Python serverless functions (`api/extract.py`) claim and process small
   task batches, then return results to PostgreSQL.
4. Failed tasks are retried with backoff; lease expiry and the one-live-attempt
   partial unique index prevent duplicate processing.

No production platform connectors are implemented here, and the 78-platform
inventory is unchanged. The existing connector `list_page` / `fetch_detail`
interface is preserved untouched.
"""

from __future__ import annotations

from jobindex.cloud.queue import claim_batch, reclaim_stale_tasks
from jobindex.cloud.worker import process_task, run_dispatch, schedule_retries

__all__ = [
    "claim_batch",
    "process_task",
    "reclaim_stale_tasks",
    "run_dispatch",
    "schedule_retries",
]
