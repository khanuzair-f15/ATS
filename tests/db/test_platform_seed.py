"""Database test: platform table seeded row-for-row from docs/platforms.md.

Marked @pytest.mark.db so it runs only when JOBINDEX_DATABASE_URL points to a live
Postgres. Skips cleanly (pytest.skip) when no database is reachable.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from jobindex.db import Platform
from jobindex.platforms import load_platform_rows

pytestmark = pytest.mark.db


async def test_platform_table_seeded_row_for_row(db_session) -> None:
    """The platform table must contain exactly the 78 rows from docs/platforms.md."""
    # Count rows
    result = await db_session.execute(select(Platform))
    rows = result.scalars().all()

    doc_rows = load_platform_rows()
    assert len(rows) == len(doc_rows), (
        f"platform table has {len(rows)} rows, expected {len(doc_rows)}"
    )

    # Build lookup by key
    db_by_key = {row.key: row for row in rows}
    doc_by_key = {row.key: row for row in doc_rows}

    # Every doc row must exist in DB with matching values
    for key, doc_row in doc_by_key.items():
        assert key in db_by_key, f"platform key {key!r} missing from database"
        db_row = db_by_key[key]

        assert db_row.display_name == doc_row.display_name, (
            f"{key}: display_name mismatch: {db_row.display_name!r} vs {doc_row.display_name!r}"
        )
        assert db_row.family == doc_row.family, f"{key}: family mismatch"
        assert db_row.platform_class.value == doc_row.platform_class, f"{key}: class mismatch"
        assert db_row.wave == doc_row.wave, f"{key}: wave mismatch"
        # transport is NULL in seed migration
        assert db_row.transport is None, f"{key}: transport should be NULL in seed"
        assert db_row.auth_kind == doc_row.auth_kind, f"{key}: auth_kind mismatch"
        assert db_row.coverage_state.value == doc_row.coverage_state, (
            f"{key}: coverage_state mismatch"
        )
        assert db_row.coverage_reason == doc_row.coverage_reason, (
            f"{key}: coverage_reason mismatch"
        )
        assert db_row.notes == doc_row.notes, f"{key}: notes mismatch"


async def test_platform_transport_is_null(db_session) -> None:
    """All platforms have transport = NULL (no probe has run)."""
    result = await db_session.execute(select(Platform))
    for row in result.scalars().all():
        assert row.transport is None, (
            f"{row.key}: transport should be NULL, got {row.transport!r}"
        )


async def test_platform_coverage_all_outstanding(db_session) -> None:
    """Phase 0: all platforms have coverage_state = 'outstanding'."""
    result = await db_session.execute(select(Platform))
    for row in result.scalars().all():
        assert row.coverage_state.value == "outstanding", (
            f"{row.key}: expected outstanding, got {row.coverage_state.value!r}"
        )
        assert row.coverage_reason is not None, f"{row.key}: coverage_reason required"


async def test_platform_unique_constraint(db_session) -> None:
    """The primary key on platform.key is enforced."""
    from sqlalchemy.exc import IntegrityError

    # Try to insert a duplicate key
    dup = Platform(
        key="greenhouse",  # exists in seed
        display_name="Duplicate",
        family="ats",
        platform_class="C1",
        wave="S3",
        auth_kind="none",
        coverage_state="outstanding",
        coverage_reason="test",
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        await db_session.commit()