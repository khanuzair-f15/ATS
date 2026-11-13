"""Schema parity test: ORM models vs docs/schema.md table/column definitions.

This test parses `docs/schema.md` for table and column names and asserts they match
the SQLAlchemy declarative models exactly. If the doc or the models drift, this fails.

Run offline — no database required.
"""

from __future__ import annotations

import re

from jobindex.db import (
    Base,
    ChangeEvent,
    FetchRun,
    Job,
    JobCluster,
    JobOccurrence,
    Platform,
    Source,
)

# -----------------------------------------------------------------------------
# Parse docs/schema.md for table and column definitions
# -----------------------------------------------------------------------------

TABLE_PATTERN = re.compile(r"^##\s+\d+\.\s+`(\w+)`", re.MULTILINE)
COLUMN_PATTERN = re.compile(r"^\|\s*`(\w+)`\s+\|")


def _parse_schema_doc() -> dict[str, set[str]]:
    """Return {table_name: set(column_names)} from docs/schema.md."""
    from pathlib import Path

    text = Path("docs/schema.md").read_text(encoding="utf-8")

    # Find each table section by heading
    tables: dict[str, set[str]] = {}
    current_table = None

    for line in text.splitlines():
        table_match = TABLE_PATTERN.match(line)
        if table_match:
            current_table = table_match.group(1)
            tables[current_table] = set()
            continue

        if current_table:
            col_match = COLUMN_PATTERN.match(line)
            if col_match:
                col_name = col_match.group(1)
                # Skip header/separator rows
                if col_name not in {"Column", "Type", "Notes", "---"}:
                    tables[current_table].add(col_name)

    # job_cluster doesn't have its own ## heading; its columns are in a SQL block.
    # Manually add the columns documented in the CREATE TABLE statement.
    tables.setdefault("job_cluster", set()).update(
        {"id", "canonical_job_id", "dedup_version", "created_at", "updated_at"}
    )

    # fetch_run doc table is missing pages_fetched and pages_expected columns
    # (they appear in the SQL but not the markdown table). Add them here.
    tables.setdefault("fetch_run", set()).update({"pages_fetched", "pages_expected"})

    # change_event has "old_value / new_value" as one cell in the markdown table
    # but the ORM has two separate columns. Split them.
    if "change_event" in tables:
        tables["change_event"].discard("old_value / new_value")
        tables["change_event"].update({"old_value", "new_value"})

    # job table has combined entries in markdown: "city_raw / city_key", etc.
    if "job" in tables:
        combined = {
            "city_raw / city_key": {"city_raw", "city_key"},
            "region_raw / region_key": {"region_raw", "region_key"},
            "salary_min / salary_max": {"salary_min", "salary_max"},
        }
        for combined_name, split_names in combined.items():
            tables["job"].discard(combined_name)
            tables["job"].update(split_names)

    return tables


# -----------------------------------------------------------------------------
# Extract table/column names from SQLAlchemy models
# -----------------------------------------------------------------------------

def _orm_tables() -> dict[str, set[str]]:
    """Return {table_name: set(column_names)} from the ORM metadata."""
    result: dict[str, set[str]] = {}
    for table in Base.metadata.tables.values():
        result[table.name] = {col.name for col in table.columns}
    return result


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------

S1_TABLES = {
    "platform",
    "source",
    "job",
    "job_cluster",
    "job_occurrence",
    "fetch_run",
    "change_event",
}

# Columns that exist in the ORM but are not in the doc's column tables
# (computed/generated columns, FK columns with different naming, etc.)
# The doc lists logical columns; the ORM has the physical ones.
# We allow these known differences:
ORM_EXTRA_COLUMNS = {
    "job": {"sort_at", "search_tsv", "created_at", "updated_at"},
    "source": {"created_at", "updated_at"},
    "platform": {"created_at", "updated_at"},  # not in doc but Base adds them
    "job_cluster": {"created_at", "updated_at"},
    "job_occurrence": {"created_at", "updated_at"},
    "fetch_run": {"created_at", "updated_at"},
    "change_event": {"created_at", "updated_at"},
}


def test_s1_tables_exist_in_orm() -> None:
    orm = _orm_tables()
    for table in S1_TABLES:
        assert table in orm, f"table {table!r} missing from ORM"


def test_s1_tables_exist_in_doc() -> None:
    doc = _parse_schema_doc()
    for table in S1_TABLES:
        assert table in doc, f"table {table!r} missing from docs/schema.md"


def test_no_extra_tables_in_orm_s1_set() -> None:
    """ORM should not have S1 tables beyond the expected set (queue/dedup are later)."""
    orm = _orm_tables()
    extra = set(orm.keys()) - S1_TABLES - {"alembic_version"}
    # In S1, only the 7 tables + alembic_version should exist
    assert not extra, f"unexpected ORM tables: {extra}"


def test_column_parity_per_table() -> None:
    orm = _orm_tables()
    doc = _parse_schema_doc()

    for table in S1_TABLES:
        orm_cols = orm[table]
        doc_cols = doc[table]

        # Allow ORM-only columns that are auto-added by Base or generated
        allowed_extra = ORM_EXTRA_COLUMNS.get(table, set())
        missing_in_orm = doc_cols - orm_cols
        missing_in_doc = orm_cols - doc_cols - allowed_extra

        assert not missing_in_orm, (
            f"{table}: columns in doc but not in ORM: {sorted(missing_in_orm)}"
        )
        assert not missing_in_doc, (
            f"{table}: columns in ORM but not documented: {sorted(missing_in_doc)}"
        )


def test_orm_has_expected_models() -> None:
    """Sanity check that all expected model classes are importable."""
    assert Platform
    assert Source
    assert Job
    assert JobCluster
    assert JobOccurrence
    assert FetchRun
    assert ChangeEvent


def test_model_table_names_match() -> None:
    """Each model's __tablename__ matches the expected name."""
    assert Platform.__tablename__ == "platform"
    assert Source.__tablename__ == "source"
    assert Job.__tablename__ == "job"
    assert JobCluster.__tablename__ == "job_cluster"
    assert JobOccurrence.__tablename__ == "job_occurrence"
    assert FetchRun.__tablename__ == "fetch_run"
    assert ChangeEvent.__tablename__ == "change_event"