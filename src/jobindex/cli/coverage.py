"""Connector coverage computation and the ``jobindex coverage`` command.

Coverage is evidence, not assertion. The four states follow ADR 0005 §A:

``outstanding``
    Nothing has been proven yet. The platform keeps a non-empty reason.
``implemented``
    A connector is explicitly registered *and* a fixture-backed test exists.
``tested``
    The registered connector has passing offline test evidence from CI.
``verified``
    A persisted, successful, complete live fetch recorded a tenant/source and
    at least one job occurrence.

A connector file on disk is never enough on its own. Registration is runtime
evidence, CI evidence is an executed-test report, and verification is a database
query over ``fetch_run`` plus ``job_occurrence``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jobindex.connectors.registry import get_registry
from jobindex.db.enums import CoverageState, FetchOutcome, SourceHealth
from jobindex.platforms import (
    PLATFORMS,
    VALID_WAVES,
    PlatformRow,
    platform_key,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[3]
CONNECTORS_DIR = REPO_ROOT / "src" / "jobindex" / "connectors"
CONNECTOR_TESTS_DIR = REPO_ROOT / "tests" / "connectors"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
INVENTORY_PATH = REPO_ROOT / "docs" / "platforms.md"
DEFAULT_REPORT_PATH = REPO_ROOT / "docs" / "coverage.md"

CI_EVIDENCE_SCHEMA_VERSION = "2.0"
REPORT_SCHEMA_VERSION = "1.0"
WAVE_ORDER = ("S3", "S7", "S8", "S10", "S11", "S12", "S13")
STATE_ORDER = (
    CoverageState.VERIFIED,
    CoverageState.TESTED,
    CoverageState.IMPLEMENTED,
    CoverageState.OUTSTANDING,
)
OUTSTANDING_DEFAULT_REASON = "not started: no connector yet (owner: unassigned)"


# -----------------------------------------------------------------------------
# Types
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CoverageEvidence:
    """Evidence behind a platform's coverage state."""

    implemented: bool = False
    tested: bool = False
    verified: bool = False
    outstanding: bool = True

    connector_path: str | None = None
    ci_evidence: dict[str, Any] | None = None
    verified_tenants: int = 0
    last_success_at: str | None = None
    sample_job_id: str | None = None
    open_failures: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PlatformCoverage:
    """Computed coverage for one inventory row."""

    platform: PlatformRow
    state: CoverageState
    reason: str
    evidence: CoverageEvidence


# -----------------------------------------------------------------------------
# File evidence
# -----------------------------------------------------------------------------


def _connector_exists(key: str) -> bool:
    """Whether a connector for *key* is explicitly registered.

    A file named ``connectors/<key>.py`` does **not** count. Registration is the
    only implementation signal the runtime can trust.
    """

    return key in get_registry()


def _has_test_file(key: str) -> bool:
    """Whether a platform-specific connector test module exists."""

    return (CONNECTOR_TESTS_DIR / f"test_{key}.py").is_file()


def _has_test_fixtures(key: str) -> bool:
    """Whether at least one captured fixture exists for the platform."""

    fixture_dir = FIXTURES_DIR / key
    return fixture_dir.is_dir() and any(path.is_file() for path in fixture_dir.rglob("*"))


def _has_fixture_backed_test(key: str, *, registered: bool) -> bool:
    """ADR 0005 ``implemented`` evidence: registered code + fixture-backed test."""

    return registered and _has_test_file(key) and _has_test_fixtures(key)


def _expected_connector_path(key: str) -> str:
    return f"src/jobindex/connectors/{key}.py"


# -----------------------------------------------------------------------------
# CI evidence
# -----------------------------------------------------------------------------


def load_ci_evidence(path: Path | None) -> dict[str, dict[str, Any]]:
    """Load executed-test evidence produced by CI.

    The file is deliberately separate from the database: adding a ``test_result``
    table for a per-build artifact would make coverage depend on schema churn.
    Expected shape (schema_version 2.0)::

        {
          "schema_version": "2.0",
          "provenance": {
            "repository": "owner/repo",
            "commit_sha": "abc123...",
            "workflow_run_id": 123456789,
            "workflow_name": "CI",
            "test_suite": "connectors"
          },
          "platforms": {
            "greenhouse": {
              "passed": 12, "failed": 0, "skipped": 1,
              "report": "https://github.com/owner/repo/actions/runs/123456789"
            }
          }
        }

    A platform is ``tested`` only when ``failed == 0`` and ``passed > 0``.
    Unknown or malformed platform keys raise instead of being ignored.

    Provenance fields are required to establish artifact traceability. A manually
    fabricated JSON file without valid provenance will be rejected.
    """

    if path is None:
        return {}

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read CI evidence {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"CI evidence {path} is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("CI evidence must be a JSON object")
    if payload.get("schema_version") != CI_EVIDENCE_SCHEMA_VERSION:
        raise ValueError(
            "CI evidence schema_version must be "
            f"{CI_EVIDENCE_SCHEMA_VERSION!r}"
        )

    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError("CI evidence must contain a 'provenance' object")

    required_provenance = (
        "repository",
        "commit_sha",
        "workflow_run_id",
        "workflow_name",
        "test_suite",
    )
    for field_name in required_provenance:
        value = provenance.get(field_name)
        if not isinstance(value, str) and field_name != "workflow_run_id":
            raise ValueError(f"CI evidence provenance.{field_name} must be a non-empty string")
        if field_name == "workflow_run_id":
            if not isinstance(value, int) or value <= 0:
                raise ValueError(
                    "CI evidence provenance.workflow_run_id must be a positive integer"
                )
        if field_name != "workflow_run_id" and not value:
            raise ValueError(f"CI evidence provenance.{field_name} must not be empty")

    raw_platforms = payload.get("platforms")
    if not isinstance(raw_platforms, dict):
        raise ValueError("CI evidence must contain a 'platforms' object")

    known_keys = {row.key for row in PLATFORMS}
    evidence: dict[str, dict[str, Any]] = {}
    for key, raw_entry in raw_platforms.items():
        if not isinstance(key, str) or key not in known_keys:
            raise ValueError(f"CI evidence names unknown platform key {key!r}")
        if not isinstance(raw_entry, dict):
            raise ValueError(f"CI evidence for {key!r} must be an object")

        counts: dict[str, int] = {}
        for field_name in ("passed", "failed", "skipped"):
            value = raw_entry.get(field_name, 0)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(
                    f"CI evidence for {key!r}: {field_name} must be a non-negative integer"
                )
            counts[field_name] = value

        report = raw_entry.get("report")
        if report is not None and not isinstance(report, str):
            raise ValueError(f"CI evidence for {key!r}: report must be a string or null")

        evidence[key] = {**counts, "report": report}

    return evidence


def _ci_passed(ci: Mapping[str, Any] | None) -> bool:
    if not ci:
        return False
    passed = ci.get("passed", 0)
    failed = ci.get("failed", 0)
    return isinstance(passed, int) and isinstance(failed, int) and passed > 0 and failed == 0


# -----------------------------------------------------------------------------
# Database evidence
# -----------------------------------------------------------------------------


def _iso_z(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


async def _collect_db_evidence(session: AsyncSession) -> dict[str, dict[str, Any]]:
    """Query verification and failure evidence from an existing session.

    VERIFIED evidence requires an explicit link from a live occurrence to a
    successful, complete fetch run for the same source via `JobOccurrence.fetch_run_id`. The
    timestamp heuristic (`last_seen_at >= started_at`) is no longer used.
    """

    from sqlalchemy import or_, select

    from jobindex.db.models import FetchRun, JobOccurrence, Source

    qualifying = (
        await session.execute(
            select(
                Source.platform_key,
                Source.tenant_key,
                FetchRun.id,
                FetchRun.started_at,
                FetchRun.finished_at,
                JobOccurrence.job_id,
                JobOccurrence.last_seen_at,
            )
            .join(FetchRun, FetchRun.source_id == Source.id)
            .join(
                JobOccurrence,
                (JobOccurrence.fetch_run_id == FetchRun.id)
                & (JobOccurrence.source_id == FetchRun.source_id),
            )
            .where(
                FetchRun.outcome == FetchOutcome.SUCCESS,
                FetchRun.finished_at.is_not(None),
                FetchRun.listing_complete.is_(True),
                FetchRun.jobs_found > 0,
                JobOccurrence.is_live.is_(True),
            )
            .order_by(
                Source.platform_key,
                Source.tenant_key,
                FetchRun.started_at,
                JobOccurrence.last_seen_at,
                JobOccurrence.job_id,
            )
        )
    ).all()

    failures = (
        await session.execute(
            select(
                Source.platform_key,
                Source.tenant_key,
                Source.health,
                Source.consecutive_failures,
            )
            .where(
                or_(
                    Source.health.in_([SourceHealth.OPEN, SourceHealth.HALF_OPEN]),
                    Source.consecutive_failures > 0,
                )
            )
            .order_by(Source.platform_key, Source.tenant_key)
        )
    ).all()

    verified: dict[str, dict[str, Any]] = {}
    for (
        key,
        tenant_key,
        run_id,
        started_at,
        finished_at,
        job_id,
        _last_seen_at,
    ) in qualifying:
        record = verified.setdefault(
            key,
            {
                "tenants": set(),
                "last_success": None,
                "last_run_id": None,
                "last_tenant": None,
                "sample_job_id": None,
            },
        )
        record["tenants"].add(tenant_key)
        completed_at = finished_at or started_at
        if record["last_success"] is None or completed_at > record["last_success"]:
            record["last_success"] = completed_at
            record["last_run_id"] = run_id
            record["last_tenant"] = tenant_key
            record["sample_job_id"] = job_id

    open_failures: dict[str, list[str]] = {}
    for key, tenant_key, health, consecutive_failures in failures:
        health_value = getattr(health, "value", str(health))
        open_failures.setdefault(key, []).append(
            f"{tenant_key}: health={health_value}, consecutive_failures={consecutive_failures}"
        )

    result: dict[str, dict[str, Any]] = {}
    for key in sorted(set(verified) | set(open_failures)):
        verified_record = verified.get(key)
        if verified_record:
            result[key] = {
                "verified": True,
                "verified_reason": (
                    f"live jobs indexed from {verified_record['last_tenant']} "
                    f"(fetch_run {verified_record['last_run_id']})"
                ),
                "verified_tenants": len(verified_record["tenants"]),
                "last_success_at": _iso_z(verified_record["last_success"]),
                "sample_job_id": (
                    str(verified_record["sample_job_id"])
                    if verified_record["sample_job_id"]
                    else None
                ),
                "open_failures": open_failures.get(key, []),
            }
        else:
            result[key] = {
                "verified": False,
                "verified_reason": None,
                "verified_tenants": 0,
                "last_success_at": None,
                "sample_job_id": None,
                "open_failures": open_failures.get(key, []),
            }

    return result


async def load_db_evidence(
    session: AsyncSession | None = None,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict[str, dict[str, Any]]:
    """Load live coverage evidence from PostgreSQL.

    Tests may pass an existing session (transaction-isolated) or a session
    factory. The CLI calls it with neither and uses the process session factory.
    """

    if session is not None:
        return await _collect_db_evidence(session)

    if session_factory is None:
        from jobindex.db.session import get_session_factory

        session_factory = get_session_factory()

    async with session_factory() as owned_session:
        return await _collect_db_evidence(owned_session)


# -----------------------------------------------------------------------------
# State computation
# -----------------------------------------------------------------------------


def _outstanding_reason(
    platform: PlatformRow,
    *,
    registered: bool,
    fixture_backed: bool,
    ci: Mapping[str, Any] | None,
) -> str:
    if fixture_backed and not _ci_passed(ci):
        if ci and ci.get("failed", 0) > 0:
            return "offline fixture suite failing in CI"
        return "fixture-backed test exists but no CI pass evidence recorded"
    if registered:
        return "connector registered but fixture-backed test is missing"
    if (CONNECTORS_DIR / f"{platform.key}.py").is_file():
        return "connector file exists but is not registered"
    reason = (platform.coverage_reason or "").strip()
    return reason or OUTSTANDING_DEFAULT_REASON


def compute_coverage_state(
    platform: PlatformRow,
    db_evidence: Mapping[str, Any] | None = None,
    ci_evidence: Mapping[str, Any] | None = None,
) -> PlatformCoverage:
    """Compute coverage for one platform.

    Priority is fixed: ``VERIFIED > TESTED > IMPLEMENTED > OUTSTANDING``.
    """

    key = platform.key
    registered = _connector_exists(key)
    fixture_backed = _has_fixture_backed_test(key, registered=registered)
    ci_passed = _ci_passed(ci_evidence)

    verified = bool(db_evidence and db_evidence.get("verified"))
    tested = verified or (fixture_backed and ci_passed)
    implemented = verified or fixture_backed
    outstanding = not implemented

    connector_path = _expected_connector_path(key) if implemented else None
    open_failures = list(db_evidence.get("open_failures", [])) if db_evidence else []

    evidence = CoverageEvidence(
        implemented=implemented,
        tested=tested,
        verified=verified,
        outstanding=outstanding,
        connector_path=connector_path,
        ci_evidence=dict(ci_evidence) if ci_evidence else None,
        verified_tenants=(
            int(db_evidence.get("verified_tenants", 0)) if verified and db_evidence else 0
        ),
        last_success_at=db_evidence.get("last_success_at") if verified and db_evidence else None,
        sample_job_id=db_evidence.get("sample_job_id") if verified and db_evidence else None,
        open_failures=open_failures,
    )

    if verified:
        assert db_evidence is not None
        state = CoverageState.VERIFIED
        reason = str(
            db_evidence.get("verified_reason") or "live jobs indexed from a recorded tenant"
        )
    elif tested:
        state = CoverageState.TESTED
        passed = ci_evidence.get("passed", 0) if ci_evidence else 0
        reason = f"offline fixture suite passed ({passed} tests)"
    elif implemented:
        state = CoverageState.IMPLEMENTED
        if ci_evidence:
            reason = _outstanding_reason(
                platform,
                registered=registered,
                fixture_backed=fixture_backed,
                ci=ci_evidence,
            )
        else:
            reason = "connector registered with a fixture-backed test"
    else:
        state = CoverageState.OUTSTANDING
        reason = _outstanding_reason(
            platform,
            registered=registered,
            fixture_backed=fixture_backed,
            ci=ci_evidence,
        )

    return PlatformCoverage(platform=platform, state=state, reason=reason, evidence=evidence)


def compute_all_coverage(
    platforms: Sequence[PlatformRow],
    db_evidence: Mapping[str, Mapping[str, Any]] | None = None,
    ci_evidence: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[PlatformCoverage]:
    """Compute coverage for every supplied platform, preserving input order."""

    return [
        compute_coverage_state(
            row,
            db_evidence.get(row.key) if db_evidence else None,
            ci_evidence.get(row.key) if ci_evidence else None,
        )
        for row in platforms
    ]


# -----------------------------------------------------------------------------
# Report rendering
# -----------------------------------------------------------------------------


def _md_cell(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|")


def _selected(
    coverage: Sequence[PlatformCoverage], wave_filter: str | None
) -> list[PlatformCoverage]:
    if wave_filter is None:
        return list(coverage)
    return [item for item in coverage if item.platform.wave == wave_filter]


def _counts(coverage: Sequence[PlatformCoverage]) -> dict[str, int]:
    counts = {state.value: 0 for state in STATE_ORDER}
    for item in coverage:
        counts[item.state.value] += 1
    return counts


def generate_markdown_report(
    coverage: Sequence[PlatformCoverage],
    *,
    wave_filter: str | None = None,
) -> str:
    """Render a deterministic Markdown report.

    No wall-clock value is emitted: two runs over the same evidence must produce
    byte-identical output. ``Data as of`` is the newest verified success.
    """

    selected = _selected(coverage, wave_filter)
    if wave_filter is not None and wave_filter not in VALID_WAVES:
        raise ValueError(f"unknown wave {wave_filter!r}")

    counts = _counts(selected)
    as_of_values = [
        item.evidence.last_success_at
        for item in selected
        if item.evidence.verified and item.evidence.last_success_at
    ]
    as_of = max(as_of_values) if as_of_values else "no successful live fetch recorded"

    lines = [
        "# Connector Coverage Report",
        "",
        "> Generated by `python -m jobindex.cli coverage`. Do not hand-edit.",
        "",
        f"Data as of: {as_of}",
    ]
    if wave_filter is not None:
        lines.append(f"Filtered by wave: {wave_filter}")
    lines.extend(["", "## Summary", "", f"- **Total platforms**: {len(selected)}"])
    for state in STATE_ORDER:
        lines.append(
            f"- **{state.value.capitalize()}**: {counts[state.value]} / {len(selected)}"
        )

    for wave in WAVE_ORDER:
        wave_platforms = [item for item in selected if item.platform.wave == wave]
        if not wave_platforms:
            continue
        lines.extend(
            [
                "",
                f"## Wave {wave}",
                "",
                "| Platform | Family | Class | State | Reason | Tenants Proven | "
                "Last Success | Sample Job | Connector Path | Open Failures |",
                "|---|---|---|---|---|---|---|---|---|---|",
            ]
        )
        for item in wave_platforms:
            platform = item.platform
            evidence = item.evidence
            tenants = str(evidence.verified_tenants) if evidence.verified else "—"
            last_success = evidence.last_success_at or "—"
            sample_job = evidence.sample_job_id or "—"
            connector = evidence.connector_path or "—"
            failures = "; ".join(evidence.open_failures) if evidence.open_failures else "—"
            lines.append(
                f"| {_md_cell(platform.display_name)} | {platform.family} | "
                f"{platform.platform_class} | {item.state.value.upper()} | "
                f"{_md_cell(item.reason)} | {tenants} | {last_success} | "
                f"{_md_cell(sample_job)} | {_md_cell(connector)} | {_md_cell(failures)} |"
            )

    return "\n".join(lines) + "\n"


def generate_json_report(
    coverage: Sequence[PlatformCoverage],
    *,
    wave_filter: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Render a versioned, deterministic JSON report."""

    selected = _selected(coverage, wave_filter)
    if wave_filter is not None and wave_filter not in VALID_WAVES:
        raise ValueError(f"unknown wave {wave_filter!r}")

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": generated_at,
        "wave": wave_filter,
        "summary": {
            "total": len(selected),
            "by_state": _counts(selected),
        },
        "platforms": [
            {
                "key": item.platform.key,
                "display_name": item.platform.display_name,
                "family": item.platform.family,
                "platform_class": item.platform.platform_class,
                "wave": item.platform.wave,
                "coverage_state": item.state.value,
                "coverage_reason": item.reason,
                "connector_path": item.evidence.connector_path,
                "verified_tenants": item.evidence.verified_tenants,
                "last_success_at": item.evidence.last_success_at,
                "sample_job_id": item.evidence.sample_job_id,
                "open_failures": list(item.evidence.open_failures),
            }
            for item in selected
        ],
    }


# -----------------------------------------------------------------------------
# Inventory Markdown regeneration
# -----------------------------------------------------------------------------


def _split_md_row(line: str) -> list[str]:
    """Split one Markdown table row, honouring escaped pipes."""

    stripped = line.strip()
    if not stripped.startswith("|"):
        return []
    body = stripped[1:]
    if body.endswith("|"):
        body = body[:-1]

    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for character in body:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
            current.append(character)
        elif character == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)
    cells.append("".join(current).strip())
    return cells


def _is_inventory_row(cells: Sequence[str]) -> bool:
    return len(cells) == 10 and bool(cells[0]) and cells[0].isdigit()


def _parse_platforms_md(path: Path) -> tuple[list[str], list[list[str]]]:
    """Parse the inventory table(s) in ``docs/platforms.md``.

    Returns the header block of the first inventory table and every inventory
    data row in document order. Both the ATS and BOARD tables are included.
    """

    lines = path.read_text(encoding="utf-8").splitlines()
    header: list[str] = []
    rows: list[list[str]] = []

    for index, line in enumerate(lines):
        cells = _split_md_row(line)
        if not header and cells[:2] == ["#", "Platform"]:
            header = lines[index : index + 2]
        if _is_inventory_row(cells):
            rows.append(cells)

    return header, rows


def _replace_last_cell(line: str, value: str) -> str:
    """Replace the final Markdown cell while preserving every other byte.

    ``line`` may include a trailing newline, which is kept untouched. The
    replacement keeps the original cell's surrounding whitespace.
    """

    newline = ""
    content = line
    for ending in ("\r\n", "\n", "\r"):
        if content.endswith(ending):
            content = content[: -len(ending)]
            newline = ending
            break

    pipes: list[int] = []
    escaped = False
    for index, character in enumerate(content):
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "|":
            pipes.append(index)

    if len(pipes) < 2 or not content.rstrip().endswith("|"):
        raise ValueError(f"not a Markdown table row: {line!r}")

    start = pipes[-2] + 1
    end = pipes[-1]
    old_cell = content[start:end]
    leading = old_cell[: len(old_cell) - len(old_cell.lstrip())]
    trailing = old_cell[len(old_cell.rstrip()) :]
    return content[:start] + leading + value + trailing + content[end:] + newline


def regenerate_platforms_md(
    path: Path,
    coverage: Sequence[PlatformCoverage],
    *,
    dry_run: bool = False,
) -> tuple[str, bool]:
    """Regenerate only the ``Coverage`` column of ``docs/platforms.md``.

    Every other byte — ordering, spacing, prose, line endings — is preserved.
    A row with no supplied coverage is an error: silently writing
    ``outstanding`` would let an accidentally filtered run corrupt the document.
    """

    by_key: dict[str, PlatformCoverage] = {}
    for item in coverage:
        if item.platform.key in by_key:
            raise ValueError(f"duplicate coverage for platform {item.platform.key!r}")
        by_key[item.platform.key] = item

    with path.open("r", encoding="utf-8", newline="") as handle:
        original = handle.read()

    new_lines: list[str] = []
    seen: set[str] = set()
    for line in original.splitlines(keepends=True):
        cells = _split_md_row(line)
        if not _is_inventory_row(cells):
            new_lines.append(line)
            continue

        key = platform_key(cells[1])
        row_coverage = by_key.get(key)
        if row_coverage is None:
            raise ValueError(f"no coverage supplied for inventory row {cells[1]!r} ({key})")
        seen.add(key)
        new_lines.append(_replace_last_cell(line, row_coverage.state.value))

    missing = set(by_key) - seen
    if missing:
        raise ValueError(f"coverage supplied for unknown platform(s): {sorted(missing)}")

    new_content = "".join(new_lines)
    changed = new_content != original

    if changed and not dry_run:
        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(new_content)

    return new_content, changed


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------


def build_coverage_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Build the ``coverage`` subcommand parser."""

    parser = subparsers.add_parser(
        "coverage",
        help="generate connector coverage reports and update the platform inventory",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help=f"Markdown report path (default: {DEFAULT_REPORT_PATH}; "
        "with --wave and no --output the report is printed)",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="write the versioned JSON report to this path",
    )
    parser.add_argument(
        "--update-platforms-md",
        action="store_true",
        help="regenerate only the Coverage column in docs/platforms.md",
    )
    parser.add_argument(
        "--test-evidence",
        type=Path,
        default=None,
        help="CI test-evidence JSON used to establish the 'tested' state",
    )
    parser.add_argument(
        "--wave",
        choices=VALID_WAVES,
        default=None,
        help="filter the report to one implementation wave",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print proposed changes without writing any file",
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="skip PostgreSQL evidence (all live-verification states stay outstanding)",
    )
    parser.set_defaults(func=cmd_coverage)
    return parser


def _write_or_print(path: Path, content: str, *, dry_run: bool) -> None:
    if dry_run:
        print(f"--- proposed {path} ---")
        print(content, end="" if content.endswith("\n") else "\n")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(content)
    print(f"Wrote {path}")


def cmd_coverage(args: argparse.Namespace) -> int:
    """Execute the coverage command."""

    platforms = list(PLATFORMS)

    ci_evidence: dict[str, dict[str, Any]] = {}
    if args.test_evidence is not None:
        try:
            ci_evidence = load_ci_evidence(args.test_evidence)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    db_evidence: dict[str, dict[str, Any]] = {}
    db_available = True
    if not args.no_db:
        try:
            db_evidence = asyncio.run(load_db_evidence())
        except Exception as exc:
            db_available = False
            if args.update_platforms_md:
                print(
                    f"error: cannot update platforms.md: database evidence unavailable ({exc})",
                    file=sys.stderr,
                )
                return 3
            print(f"warning: database evidence unavailable: {exc}", file=sys.stderr)

    all_coverage = compute_all_coverage(platforms, db_evidence, ci_evidence)

    markdown = generate_markdown_report(all_coverage, wave_filter=args.wave)

    write_path: Path | None
    if args.output is not None:
        write_path = args.output
    elif args.wave is None:
        write_path = DEFAULT_REPORT_PATH
    else:
        write_path = None

    if write_path is None:
        print(markdown, end="")
    else:
        _write_or_print(write_path, markdown, dry_run=args.dry_run)

    if args.json is not None:
        payload = generate_json_report(all_coverage, wave_filter=args.wave)
        json_text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        _write_or_print(args.json, json_text, dry_run=args.dry_run)

    if args.update_platforms_md:
        if not db_available and not args.no_db:
            # Already handled above, but double-check
            print(
                "error: cannot update platforms.md without database evidence",
                file=sys.stderr,
            )
            return 3
        if args.no_db:
            print(
                "error: --update-platforms-md requires database evidence; "
                "cannot use --no-db",
                file=sys.stderr,
            )
            return 3

        try:
            new_content, changed = regenerate_platforms_md(
                INVENTORY_PATH,
                all_coverage,
                dry_run=args.dry_run,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

        if args.dry_run:
            print(f"--- proposed {INVENTORY_PATH} ---")
            print(new_content, end="" if new_content.endswith("\n") else "\n")
        elif changed:
            print(f"Updated {INVENTORY_PATH}")
        else:
            print(f"No changes to {INVENTORY_PATH}")

    return 0


def coverage_main(argv: Sequence[str] | None = None) -> int:
    """Standalone entry point used by tests; the main CLI wires the parser."""

    parser = argparse.ArgumentParser(prog="jobindex coverage")
    parser.add_argument("--output", "-o", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--update-platforms-md", action="store_true")
    parser.add_argument("--test-evidence", type=Path, default=None)
    parser.add_argument("--wave", choices=VALID_WAVES, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-db", action="store_true")
    return cmd_coverage(parser.parse_args(argv))


__all__ = [
    "CoverageEvidence",
    "PlatformCoverage",
    "compute_all_coverage",
    "compute_coverage_state",
    "generate_json_report",
    "generate_markdown_report",
    "load_ci_evidence",
    "load_db_evidence",
    "regenerate_platforms_md",
]
