"""Add fetch_run_id to job_occurrence for exact run-to-occurrence association.

This migration adds a foreign key from job_occurrence to fetch_run, enabling
precise verification evidence: a VERIFIED coverage state requires at least one
live occurrence that was recorded by a specific successful, complete fetch run.

Revision ID: 0003_occurrence_fetch_run
Revises: 0002_seed_platforms
Create Date: 2026-11-17
"""

from __future__ import annotations

from alembic import op

revision: str = "0003_occurrence_fetch_run"
down_revision: str = "0002_seed_platforms"
branch_labels: str | None = None
depends_on: str | None = None


UPGRADE: list[str] = [
    # Add fetch_run_id column to job_occurrence, nullable for existing rows
    "ALTER TABLE job_occurrence ADD COLUMN fetch_run_id uuid REFERENCES fetch_run(id) ON DELETE SET NULL",
    # Index for querying occurrences by fetch run
    "CREATE INDEX occurrence_fetch_run ON job_occurrence (fetch_run_id) WHERE fetch_run_id IS NOT NULL",
]


DOWNGRADE: list[str] = [
    "DROP INDEX IF EXISTS occurrence_fetch_run",
    "ALTER TABLE job_occurrence DROP COLUMN IF EXISTS fetch_run_id",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)