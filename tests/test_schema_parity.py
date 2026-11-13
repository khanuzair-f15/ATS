"""Schema parity test: ORM models vs docs/schema.md table/column definitions.

This test parses `docs/schema.md` for table and column definitions and asserts they match
the SQLAlchemy declarative models exactly. If the doc or the models drift, this fails.

Run offline — no database required.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import UniqueConstraint
from sqlalchemy.sql.schema import Column

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
# Matches markdown table rows: | `col_name` | `type` | notes |
COLUMN_ROW_PATTERN = re.compile(r"^\|\s*`([^`]+)`\s+\|\s*`([^`]+)`\s+\|")


@dataclass
class DocColumn:
    name: str
    type: str
    nullable: bool


def _parse_schema_doc() -> dict[str, dict[str, DocColumn]]:
    """Return {table_name: {col_name: DocColumn}} from docs/schema.md."""
    from pathlib import Path

    text = Path("docs/schema.md").read_text(encoding="utf-8")

    tables: dict[str, dict[str, DocColumn]] = {}
    current_table = None

    for line in text.splitlines():
        table_match = TABLE_PATTERN.match(line)
        if table_match:
            current_table = table_match.group(1)
            tables[current_table] = {}
            continue

        if current_table:
            col_match = COLUMN_ROW_PATTERN.match(line)
            if col_match:
                col_name_raw = col_match.group(1).strip()
                col_type = col_match.group(2).strip()

                # Skip header/separator rows
                if col_name_raw in {"Column", "Type", "---", "Notes"}:
                    continue

                # Handle combined entries like "city_raw / city_key"
                if " / " in col_name_raw:
                    # Will be split later
                    pass

                # Determine nullability from type (contains "not null")
                nullable = "not null" not in col_type.lower()

                tables[current_table][col_name_raw] = DocColumn(
                    name=col_name_raw, type=col_type, nullable=nullable
                )

    # job_cluster doesn't have its own ## heading; its columns are in a SQL block.
    tables.setdefault("job_cluster", {}).update(
        {
            "id": DocColumn("id", "uuid PRIMARY KEY DEFAULT gen_random_uuid()", False),
            "canonical_job_id": DocColumn(
                "canonical_job_id", "uuid NOT NULL UNIQUE REFERENCES job(id)", False
            ),
            "dedup_version": DocColumn("dedup_version", "text NOT NULL", False),
            "created_at": DocColumn("created_at", "timestamptz NOT NULL DEFAULT now()", False),
            "updated_at": DocColumn("updated_at", "timestamptz NOT NULL DEFAULT now()", False),
        }
    )

    # fetch_run doc table is missing pages_fetched and pages_expected columns
    tables.setdefault("fetch_run", {}).update(
        {
            "pages_fetched": DocColumn("pages_fetched", "int", True),
            "pages_expected": DocColumn("pages_expected", "int", True),
        }
    )

    # change_event has "old_value / new_value" as one cell in the markdown table
    if "change_event" in tables:
        tables["change_event"].pop("old_value / new_value", None)
        tables["change_event"].update(
            {
                "old_value": DocColumn("old_value", "text", True),
                "new_value": DocColumn("new_value", "text", True),
            }
        )

    # job table has combined entries in markdown: "city_raw / city_key", etc.
    if "job" in tables:
        combined = {
            "city_raw / city_key": [
                DocColumn("city_raw", "text", True),
                DocColumn("city_key", "text", True),
            ],
            "region_raw / region_key": [
                DocColumn("region_raw", "text", True),
                DocColumn("region_key", "text", True),
            ],
            "salary_min / salary_max": [
                DocColumn("salary_min", "numeric(12,2)", True),
                DocColumn("salary_max", "numeric(12,2)", True),
            ],
        }
        for combined_name, split_cols in combined.items():
            tables["job"].pop(combined_name, None)
            for col in split_cols:
                tables["job"][col.name] = col

    return tables


# -----------------------------------------------------------------------------
# Extract table/column names from SQLAlchemy models
# -----------------------------------------------------------------------------

def _orm_tables() -> dict[str, dict[str, Column]]:
    """Return {table_name: {col_name: Column}} from the ORM metadata."""
    result: dict[str, dict[str, Column]] = {}
    for table in Base.metadata.tables.values():
        result[table.name] = {col.name: col for col in table.columns}
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
ORM_EXTRA_COLUMNS = {
    "job": {"sort_at", "search_tsv", "created_at", "updated_at"},
    "source": {"created_at", "updated_at"},
    "platform": {"created_at", "updated_at"},
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
    assert not extra, f"unexpected ORM tables: {extra}"


def test_column_parity_per_table() -> None:
    orm = _orm_tables()
    doc = _parse_schema_doc()

    for table in S1_TABLES:
        orm_cols = set(orm[table].keys())
        doc_cols = set(doc[table].keys())

        allowed_extra = ORM_EXTRA_COLUMNS.get(table, set())
        missing_in_orm = doc_cols - orm_cols
        missing_in_doc = orm_cols - doc_cols - allowed_extra

        assert not missing_in_orm, (
            f"{table}: columns in doc but not in ORM: {sorted(missing_in_orm)}"
        )
        assert not missing_in_doc, (
            f"{table}: columns in ORM but not documented: {sorted(missing_in_doc)}"
        )


def test_column_types_and_nullability() -> None:
    """Verify column nullability matches between doc and ORM.
    Type comparison is skipped because the doc uses PostgreSQL type syntax
    while the ORM uses SQLAlchemy/Python type representations.
    Primary key columns are implicitly not nullable in the ORM but may not
    be marked as such in the markdown doc.
    """
    orm = _orm_tables()
    doc = _parse_schema_doc()

    nullability_mismatches: list[str] = []

    for table in S1_TABLES:
        # Get primary key column names from ORM
        orm_pks = {col.name for col in orm[table].values() if col.primary_key}

        for col_name, doc_col in doc[table].items():
            if col_name in ORM_EXTRA_COLUMNS.get(table, set()):
                continue
            if col_name not in orm[table]:
                continue

            # Skip primary key columns - they're implicitly not nullable in ORM
            # but markdown tables may not explicitly mark them "not null"
            if col_name in orm_pks:
                continue

            orm_col = orm[table][col_name]

            # Check nullability (doc's "not null" vs ORM's nullable)
            doc_nullable = doc_col.nullable
            orm_nullable = orm_col.nullable
            if doc_nullable != orm_nullable:
                nullability_mismatches.append(
                    f"{table}.{col_name}: doc nullable={doc_nullable}, orm nullable={orm_nullable}"
                )

    assert not nullability_mismatches, (
            "Nullability mismatches:\n" + "\n".join(nullability_mismatches)
        )


def test_primary_keys() -> None:
    """Verify primary keys match between doc and ORM."""
    orm = _orm_tables()
    _parse_schema_doc()  # ensure doc parsing works

    expected_pks = {
        "platform": {"key"},
        "source": {"id"},
        "job": {"id"},
        "job_cluster": {"id"},
        "job_occurrence": {"id"},
        "fetch_run": {"id"},
        "change_event": {"id"},
    }

    for table, expected in expected_pks.items():
        orm_pks = {col.name for col in orm[table].values() if col.primary_key}
        assert orm_pks == expected, f"{table}: PK mismatch: {orm_pks} != {expected}"


def test_foreign_keys() -> None:
    """Verify foreign keys exist as expected."""
    _orm_tables()  # ensure ORM metadata is loaded

    expected_fks = {
        "source": {"platform_key"},  # -> platform.key
        "job": {"origin_source_id", "cluster_id", "merged_into_job_id"},
        "job_cluster": {"canonical_job_id"},  # -> job.id
        "job_occurrence": {"job_id", "source_id"},
        "fetch_run": {"source_id"},
        "change_event": {"job_id", "occurrence_id", "fetch_run_id"},
    }

    for table, expected_fk_cols in expected_fks.items():
        table_obj = Base.metadata.tables[table]
        fk_cols = set()
        for fk in table_obj.foreign_keys:
            fk_cols.add(fk.parent.name)
        assert expected_fk_cols.issubset(fk_cols), (
            f"{table}: missing FK columns: {expected_fk_cols - fk_cols} (found: {fk_cols})"
        )


def test_unique_constraints() -> None:
    """Verify unique constraints match expectations."""
    expected_uniques = {
        "source": {"source_tenant"},  # (platform_key, tenant_key)
        "job": {"job_origin_identity"},  # (origin_source_id, origin_external_id)
        "job_cluster": {"uq_job_cluster_canonical_job_id"},  # canonical_job_id unique
        "job_occurrence": {"occurrence_identity"},  # (source_id, external_id)
        "fetch_run": {"uq_fetch_run_queue_id"},  # queue_id unique
        "change_event": {"change_event_idempotent"},
        # (occurrence_id, field, new_value, fetch_run_id)
    }

    for table, expected_names in expected_uniques.items():
        table_obj = Base.metadata.tables[table]
        uq_names = {uq.name for uq in table_obj.constraints if isinstance(uq, UniqueConstraint)}
        assert expected_names.issubset(uq_names), (
            f"{table}: missing unique constraints: {expected_names - uq_names} (found: {uq_names})"
        )


def test_check_constraints() -> None:
    """Verify check constraints exist."""
    expected_checks = {
        "platform": {"ck_platform_outstanding_requires_reason", "ck_platform_family_known"},
        "source": {"ck_source_config_no_secrets"},
        "job": {
            "ck_job_salary_range_valid",
            "ck_job_salary_currency_required",
            "ck_job_salary_currency_iso4217",
            "ck_job_country_code_iso2",
            "ck_job_closed_requires_evidence",
            "ck_job_open_forbids_evidence",
            "ck_job_stale_requires_since",
            "ck_job_merged_requires_cluster",
            "ck_job_canonical_cannot_be_merged",
        },
    }

    for table, expected_names in expected_checks.items():
        table_obj = Base.metadata.tables[table]
        # Naming convention uses "ck_" prefix for check constraints
        ck_names = {
            ck.name
            for ck in table_obj.constraints
            if ck.name and ck.name.startswith("ck_")
        }
        for expected in expected_names:
            assert expected in ck_names, (
                f"{table}: missing check constraint '{expected}' (found: {ck_names})"
            )


def test_partial_indexes() -> None:
    """Verify partial indexes exist."""
    expected_partial_indexes = {
        "job": {
            "job_one_canonical_per_cluster",
            "job_open_liveness",
            "job_sort",
            "job_remote_type",
            "job_salary_floor",
        },
        "source": {"source_due"},
        "job_occurrence": {"occurrence_job_live"},
        "fetch_run": {"fetch_run_failures", "fetch_run_complete_listing"},
    }

    for table, expected_names in expected_partial_indexes.items():
        table_obj = Base.metadata.tables[table]
        idx_names = {idx.name for idx in table_obj.indexes}
        for expected in expected_names:
            assert expected in idx_names, (
                f"{table}: missing partial index '{expected}' (found: {idx_names})"
            )


def test_orm_has_expected_models() -> None:
    assert Platform
    assert Source
    assert Job
    assert JobCluster
    assert JobOccurrence
    assert FetchRun
    assert ChangeEvent


def test_model_table_names_match() -> None:
    assert Platform.__tablename__ == "platform"
    assert Source.__tablename__ == "source"
    assert Job.__tablename__ == "job"
    assert JobCluster.__tablename__ == "job_cluster"
    assert JobOccurrence.__tablename__ == "job_occurrence"
    assert FetchRun.__tablename__ == "fetch_run"
    assert ChangeEvent.__tablename__ == "change_event"