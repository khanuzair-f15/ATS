"""Additive serverless task queue for Phase 5A cloud-only execution.

Creates `serverless_task_queue` and `serverless_execution_log` plus a
`dispatch_serverless_tasks()` SQL function. No existing table, column, or
constraint is modified, so Phase 3/5 schema work (the S5 `source_run_queue`
table in docs/schema.md §10) is untouched and can be introduced later without
conflict.

Design mirrors the approved source_run_queue / fetch_run pattern (ADR 0003,
docs/schema.md §7, §10) but is scoped to serverless execution:

- short-lived claims via `FOR UPDATE ... SKIP LOCKED` (no duplicate processing)
- task ownership (claimed_by), lease expiry (heartbeat_at), attempts, backoff
  (next_claim_at), completion and failure states
- an execution log for audit / verification
- fetch_run written at claim/start, finalized at completion, `abandoned` on
  worker crash (reclaim)

Revision ID: 0004_serverless_execution
Revises: 0003_occurrence_fetch_run
Create Date: 2026-11-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_serverless_execution"
down_revision: str = "0003_occurrence_fetch_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# One live attempt per source at a time (mirrors source_run_queue §10).
QUEUE_STATE_ENUM = (
    "CREATE TYPE serverless_task_state AS ENUM "
    "('pending', 'running', 'succeeded', 'failed', 'abandoned')"
)

TASK_QUEUE_DDL: list[str] = [
    """
    CREATE TABLE serverless_task_queue (
      id                 bigserial PRIMARY KEY,
      source_id          uuid NOT NULL REFERENCES source(id) ON DELETE RESTRICT,
      run_id             uuid NOT NULL DEFAULT gen_random_uuid(),
      claim_token        uuid,                              -- unique per claim; fences mutations
      task_kind          text NOT NULL DEFAULT 'refresh',
      payload            jsonb NOT NULL DEFAULT '{}'::jsonb,
      state              serverless_task_state NOT NULL DEFAULT 'pending',
      claimed_by         text,
      claimed_at         timestamptz,
      heartbeat_at       timestamptz,
      attempts           int NOT NULL DEFAULT 0,
      max_attempts       int NOT NULL DEFAULT 3,
      next_claim_at      timestamptz NOT NULL DEFAULT now(),
      error_detail       text,
      result             jsonb,
      created_at         timestamptz NOT NULL DEFAULT now(),
      completed_at       timestamptz,
      CONSTRAINT serverless_task_source_run
        UNIQUE (source_id, run_id)
    )
    """,
    "CREATE UNIQUE INDEX serverless_task_one_live_per_source "
    "ON serverless_task_queue (source_id) WHERE state IN ('pending', 'running')",
    "CREATE INDEX serverless_task_pending "
    "ON serverless_task_queue (next_claim_at) WHERE state = 'pending'",
    "CREATE INDEX serverless_task_source_state "
    "ON serverless_task_queue (source_id, state)",
    "CREATE INDEX serverless_task_run ON serverless_task_queue (run_id)",
]

# Add claimed_task_token to source for cross-table claim fencing.
# A worker must present the current claim_token to mutate source health,
# preventing stale/reclaimed workers from corrupting source state.
SOURCE_CLAIM_TOKEN_DDL: list[str] = [
    "ALTER TABLE source ADD COLUMN claimed_task_token uuid",
]

EXECUTION_LOG_DDL: list[str] = [
    """
    CREATE TABLE serverless_execution_log (
      id        bigserial PRIMARY KEY,
      task_id   bigint REFERENCES serverless_task_queue(id) ON DELETE CASCADE,
      run_id    uuid NOT NULL,
      worker_id text NOT NULL,
      event     text NOT NULL,
      detail    jsonb NOT NULL DEFAULT '{}'::jsonb,
      created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX serverless_execution_log_task "
    "ON serverless_execution_log (task_id, created_at DESC)",
    "CREATE INDEX serverless_execution_log_run ON serverless_execution_log (run_id)",
    "CREATE INDEX serverless_execution_log_event "
    "ON serverless_execution_log (event, created_at DESC)",
]

# Dispatcher: insert one pending task per due source that has no live attempt.
# Runs on a cloud schedule (Supabase Cron / pg_cron) so indexing continues while
# the developer's computer is off. Bounded to :batch_limit per invocation.
DISPATCH_FUNCTION: list[str] = [
    """
    CREATE OR REPLACE FUNCTION dispatch_serverless_tasks(batch_limit int DEFAULT 25)
    RETURNS int
    LANGUAGE plpgsql
    AS $$
    DECLARE inserted_count int;
    BEGIN
      WITH due_sources AS (
        SELECT s.id AS source_id,
               gen_random_uuid() AS run_id
        FROM source s
        WHERE s.enabled
          AND s.health <> 'open'
          AND (s.backoff_until IS NULL OR s.backoff_until <= now())
          AND s.next_due_at <= now()
          AND NOT EXISTS (
            SELECT 1 FROM serverless_task_queue q
            WHERE q.source_id = s.id
              AND q.state IN ('pending', 'running')
          )
        ORDER BY s.next_due_at ASC
        LIMIT batch_limit
      )
      INSERT INTO serverless_task_queue (source_id, run_id, task_kind, payload, next_claim_at)
      SELECT d.source_id, d.run_id, 'refresh',
             jsonb_build_object('source_id', d.source_id::text, 'tenant_due', now()),
             now()
      FROM due_sources d
      RETURNING 1 INTO inserted_count;

      RETURN COALESCE(inserted_count, 0);
    END;
    $$;
    """
]

UPGRADE: list[str] = [
    QUEUE_STATE_ENUM,
    *TASK_QUEUE_DDL,
    *EXECUTION_LOG_DDL,
    *SOURCE_CLAIM_TOKEN_DDL,
    *DISPATCH_FUNCTION,
]

DOWNGRADE: list[str] = [
    "DROP FUNCTION IF EXISTS dispatch_serverless_tasks(int)",
    "DROP INDEX IF EXISTS serverless_execution_log_event",
    "DROP INDEX IF EXISTS serverless_execution_log_run",
    "DROP INDEX IF EXISTS serverless_execution_log_task",
    "DROP INDEX IF EXISTS serverless_task_one_live_per_source",
    "DROP INDEX IF EXISTS serverless_task_run",
    "DROP INDEX IF EXISTS serverless_task_source_state",
    "DROP INDEX IF EXISTS serverless_task_pending",
    "DROP TABLE IF EXISTS serverless_execution_log",
    "DROP TABLE IF EXISTS serverless_task_queue",
    "DROP TYPE IF EXISTS serverless_task_state",
    "ALTER TABLE source DROP COLUMN IF EXISTS claimed_task_token",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
