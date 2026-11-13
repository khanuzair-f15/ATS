"""Seed `platform` from docs/platforms.md.

Revision ID: 0002_seed_platforms
Revises: 0001_baseline
Create Date: 2026-11-13

The document is the authority, so the seed parses it rather than carrying a second
hand-written list. That is a deliberate trade: a migration that reads a file is only
reproducible if the file is unchanged, so `tests/test_platform_inventory.py` asserts the
document still holds exactly 78 brief-exact rows and `tests/db/test_platform_seed.py`
asserts the table agrees row-for-row.

`transport` is NULL for every row: no probe has run, and `docs/schema.md` §3 says the
column is null until one has. Guessing it here would be fabricated evidence.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from jobindex.platforms import load_platform_rows

revision: str = "0002_seed_platforms"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INSERT = sa.text(
    """
    INSERT INTO platform (
        key, display_name, family, class, wave, transport,
        auth_kind, coverage_state, coverage_reason, notes
    ) VALUES (
        :key, :display_name, :family, :class, :wave, NULL,
        :auth_kind, :coverage_state, :coverage_reason, :notes
    )
    ON CONFLICT (key) DO NOTHING
    """
)


def upgrade() -> None:
    rows = load_platform_rows()
    if len(rows) != 78:
        raise RuntimeError(f"expected 78 platform rows in docs/platforms.md, got {len(rows)}")
    op.get_bind().execute(
        INSERT,
        [
            {
                "key": row.key,
                "display_name": row.display_name,
                "family": row.family,
                "class": row.platform_class,
                "wave": row.wave,
                "auth_kind": row.auth_kind,
                "coverage_state": row.coverage_state,
                "coverage_reason": row.coverage_reason,
                "notes": row.notes,
            }
            for row in rows
        ],
    )


def downgrade() -> None:
    keys = [row.key for row in load_platform_rows()]
    op.get_bind().execute(
        sa.text("DELETE FROM platform WHERE key = ANY(:keys)"), {"keys": keys}
    )
