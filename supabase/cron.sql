-- Supabase Cron (pg_cron) for the Phase 5A Vercel + Supabase
-- cloud-only execution path.
--
-- pg_cron is the SINGLE scheduler for this path. It runs inside Supabase
-- PostgreSQL, so indexing continues while the developer's computer is off.
--
-- Two jobs are scheduled here (both active by default):
--   1. jobindex-dispatch-serverless-tasks  -- enqueues pending tasks (every minute)
--   2. jobindex-trigger-vercel-worker      -- triggers the Vercel worker via pg_net (every minute)
--
-- Only ONE scheduler should be active. Do NOT also enable the `crons` entry in
-- vercel.json; that would cause duplicate worker invocations.
--
-- Apply once to the Supabase project (SQL Editor). Requires the pg_cron and
-- pg_net extensions (enabled by default on Supabase; otherwise ask support or
-- enable via `CREATE EXTENSION`).
--
-- Set JOBINDEX_CRON_SECRET in both the Supabase project (for the pg_net header
-- and `current_setting('jobindex.cron_secret', true)`) and the Vercel project
-- (for handler verification) so the worker endpoint is authenticated.
--
-- To store the secret in the database for current_setting():
--   ALTER DATABASE postgres SET jobindex.cron_secret = '<your-secret>';
--   SELECT pg_reload_conf();

-- ---------------------------------------------------------------------------
-- 1. Dispatcher: enqueue at most 25 pending tasks per minute.
--    The dispatcher SQL function lives in migration 0004.
-- ---------------------------------------------------------------------------
SELECT cron.schedule(
  'jobindex-dispatch-serverless-tasks',
  '* * * * *',
  'SELECT dispatch_serverless_tasks(25);'
);

-- ---------------------------------------------------------------------------
-- 2. Trigger the Vercel worker after dispatching.
--    Runs every minute so indexing is fully automatic in the cloud.
-- ---------------------------------------------------------------------------
SELECT cron.schedule(
  'jobindex-trigger-vercel-worker',
  '* * * * *',
  $$
  SELECT net.http_post(
    'https://<vercel-project>.vercel.app/api/extract',
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'Authorization', 'Bearer ' || current_setting('jobindex.cron_secret', true)
    ),
    body := '{"batch_size": 5}'::jsonb
  );
  $$
);

-- ---------------------------------------------------------------------------
-- 3. Operational SQL:
--    SELECT * FROM cron.job;              -- list scheduled jobs
--    SELECT cron.unschedule('jobindex-dispatch-serverless-tasks');  -- remove a job
--    SELECT cron.unschedule('jobindex-trigger-vercel-worker');
-- ---------------------------------------------------------------------------

-- Notes:
-- - pg_cron fires at minute resolution. Sub-minute schedules are supported by
--   pg_cron via step values (e.g. '*/1 * * * *') but are not required by Phase
--   5A, whose refresh tiers are 6h/12h/24h/72h. Sub-minute scheduling is not
--   required for this prototype.
-- - The dispatcher only ENQUEUES tasks; it does not process them. Processing is
--   the Vercel worker's job (short-lived, bounded batch), which keeps each
--   function invocation well within Vercel's duration limits.
-- - One live attempt per source is enforced by the partial unique index on
--   serverless_task_queue (state IN ('pending','running')), so the cron can run
--   every minute without creating duplicate work.
-- - The Vercel worker endpoint (api/extract.handler) requires the cron secret;
--   unauthenticated triggers return 401, and a missing/empty secret is
--   fail-closed (all protected requests rejected with 503).
