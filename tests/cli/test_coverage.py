"""Tests for connector coverage computation, rendering, and the CLI."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from jobindex.cli.coverage import (
    CoverageEvidence,
    PlatformCoverage,
    _parse_platforms_md,
    build_coverage_parser,
    compute_all_coverage,
    compute_coverage_state,
    generate_json_report,
    generate_markdown_report,
    load_ci_evidence,
    regenerate_platforms_md,
)
from jobindex.db.enums import CoverageState
from jobindex.platforms import PLATFORMS, PlatformRow, load_platform_rows


def make_platform(
    key: str = "testplatform",
    display_name: str = "Test Platform",
    *,
    wave: str = "S7",
    platform_class: str = "C1",
    family: str = "ats",
    coverage_reason: str = "not started: no connector yet (owner: unassigned)",
) -> PlatformRow:
    return PlatformRow(
        key=key,
        display_name=display_name,
        family=family,
        platform_class=platform_class,
        wave=wave,
        auth_kind="none",
        coverage_state="outstanding",
        coverage_reason=coverage_reason,
        discovery="test discovery",
        risk="L",
    )


def coverage_for(
    platform: PlatformRow,
    state: CoverageState,
    *,
    reason: str = "test",
    evidence: CoverageEvidence | None = None,
) -> PlatformCoverage:
    return PlatformCoverage(
        platform=platform,
        state=state,
        reason=reason,
        evidence=evidence or CoverageEvidence(),
    )


class TestCoverageEvidence:
    """Tests for CoverageEvidence dataclass."""

    def test_default_values(self):
        evidence = CoverageEvidence()
        assert evidence.implemented is False
        assert evidence.tested is False
        assert evidence.verified is False
        assert evidence.outstanding is True
        assert evidence.connector_path is None
        assert evidence.ci_evidence is None
        assert evidence.verified_tenants == 0
        assert evidence.last_success_at is None
        assert evidence.sample_job_id is None
        assert evidence.open_failures == []

    def test_custom_values(self):
        evidence = CoverageEvidence(
            implemented=True,
            tested=True,
            verified=True,
            outstanding=False,
            connector_path="test/path.py",
            ci_evidence={"test": "data"},
            verified_tenants=2,
            last_success_at="2024-01-01",
            sample_job_id="job123",
            open_failures=["failure1", "failure2"],
        )
        assert evidence.implemented is True
        assert evidence.tested is True
        assert evidence.verified is True
        assert evidence.outstanding is False
        assert evidence.connector_path == "test/path.py"
        assert evidence.ci_evidence == {"test": "data"}
        assert evidence.verified_tenants == 2
        assert evidence.last_success_at == "2024-01-01"
        assert evidence.sample_job_id == "job123"
        assert evidence.open_failures == ["failure1", "failure2"]

    def test_default_failure_list_is_not_shared(self):
        first = CoverageEvidence()
        second = CoverageEvidence()
        first.open_failures.append("x")
        assert second.open_failures == []


class TestPlatformCoverage:
    """Tests for PlatformCoverage dataclass."""

    def test_platform_coverage_creation(self):
        platform = make_platform()
        coverage = coverage_for(
            platform,
            CoverageState.IMPLEMENTED,
            reason="connector code exists",
            evidence=CoverageEvidence(implemented=True),
        )
        assert coverage.platform == platform
        assert coverage.state == CoverageState.IMPLEMENTED
        assert coverage.reason == "connector code exists"
        assert coverage.evidence.implemented is True


class TestComputeCoverageState:
    """Coverage states must follow evidence, never file names alone."""

    def setup_method(self):
        self.platform = make_platform()

    def test_outstanding_no_connector(self):
        with patch("jobindex.cli.coverage._connector_exists", return_value=False):
            coverage = compute_coverage_state(self.platform)
        assert coverage.state == CoverageState.OUTSTANDING
        assert coverage.evidence.implemented is False
        assert coverage.evidence.tested is False
        assert coverage.evidence.verified is False
        assert coverage.evidence.outstanding is True
        assert coverage.reason

    def test_connector_file_alone_is_not_coverage(self, tmp_path, monkeypatch):
        (tmp_path / "testplatform.py").write_text("# not registered\n")
        monkeypatch.setattr("jobindex.cli.coverage.CONNECTORS_DIR", tmp_path)
        with patch("jobindex.cli.coverage._connector_exists", return_value=False):
            coverage = compute_coverage_state(self.platform)
        assert coverage.state == CoverageState.OUTSTANDING
        assert coverage.reason == "connector file exists but is not registered"

    def test_registered_without_fixture_backed_test_is_outstanding(self):
        with (
            patch("jobindex.cli.coverage._connector_exists", return_value=True),
            patch("jobindex.cli.coverage._has_test_file", return_value=False),
            patch("jobindex.cli.coverage._has_test_fixtures", return_value=False),
        ):
            coverage = compute_coverage_state(self.platform)
        assert coverage.state == CoverageState.OUTSTANDING
        assert coverage.reason == "connector registered but fixture-backed test is missing"

    def test_registered_with_fixtures_but_no_test_file_is_outstanding(self):
        with (
            patch("jobindex.cli.coverage._connector_exists", return_value=True),
            patch("jobindex.cli.coverage._has_test_file", return_value=False),
            patch("jobindex.cli.coverage._has_test_fixtures", return_value=True),
        ):
            coverage = compute_coverage_state(self.platform)
        assert coverage.state == CoverageState.OUTSTANDING

    def test_registered_with_fixture_backed_test_is_implemented(self):
        with (
            patch("jobindex.cli.coverage._connector_exists", return_value=True),
            patch("jobindex.cli.coverage._has_test_file", return_value=True),
            patch("jobindex.cli.coverage._has_test_fixtures", return_value=True),
        ):
            coverage = compute_coverage_state(self.platform)
        assert coverage.state == CoverageState.IMPLEMENTED
        assert coverage.evidence.implemented is True
        assert coverage.evidence.tested is False
        assert coverage.evidence.verified is False
        assert coverage.evidence.connector_path == "src/jobindex/connectors/testplatform.py"
        assert coverage.reason == "connector registered with a fixture-backed test"

    def test_tested_requires_ci_pass(self):
        ci = {"passed": 12, "failed": 0, "skipped": 0, "report": "https://ci/1"}
        with (
            patch("jobindex.cli.coverage._connector_exists", return_value=True),
            patch("jobindex.cli.coverage._has_test_file", return_value=True),
            patch("jobindex.cli.coverage._has_test_fixtures", return_value=True),
        ):
            coverage = compute_coverage_state(self.platform, ci_evidence=ci)
        assert coverage.state == CoverageState.TESTED
        assert coverage.evidence.implemented is True
        assert coverage.evidence.tested is True
        assert coverage.evidence.verified is False
        assert coverage.evidence.ci_evidence == ci
        assert coverage.reason == "offline fixture suite passed (12 tests)"

    def test_tested_requires_fixture_backed_test(self):
        ci = {"passed": 12, "failed": 0}
        with (
            patch("jobindex.cli.coverage._connector_exists", return_value=True),
            patch("jobindex.cli.coverage._has_test_file", return_value=False),
            patch("jobindex.cli.coverage._has_test_fixtures", return_value=True),
        ):
            coverage = compute_coverage_state(self.platform, ci_evidence=ci)
        assert coverage.state == CoverageState.OUTSTANDING

    def test_failing_ci_does_not_establish_tested(self):
        ci = {"passed": 10, "failed": 2, "skipped": 0}
        with (
            patch("jobindex.cli.coverage._connector_exists", return_value=True),
            patch("jobindex.cli.coverage._has_test_file", return_value=True),
            patch("jobindex.cli.coverage._has_test_fixtures", return_value=True),
        ):
            coverage = compute_coverage_state(self.platform, ci_evidence=ci)
        assert coverage.state == CoverageState.IMPLEMENTED
        assert coverage.reason == "offline fixture suite failing in CI"

    def test_zero_passed_ci_is_not_tested(self):
        ci = {"passed": 0, "failed": 0, "skipped": 9}
        with (
            patch("jobindex.cli.coverage._connector_exists", return_value=True),
            patch("jobindex.cli.coverage._has_test_file", return_value=True),
            patch("jobindex.cli.coverage._has_test_fixtures", return_value=True),
        ):
            coverage = compute_coverage_state(self.platform, ci_evidence=ci)
        assert coverage.state == CoverageState.IMPLEMENTED
        assert coverage.reason == "fixture-backed test exists but no CI pass evidence recorded"

    def test_verified_from_db_evidence(self):
        db_evidence = {
            "verified": True,
            "verified_reason": "live jobs indexed from tenant-x (fetch_run run-1)",
            "verified_tenants": 2,
            "last_success_at": "2024-01-01T12:00:00Z",
            "sample_job_id": "job123",
            "open_failures": ["tenant-y: health=open, consecutive_failures=3"],
        }
        with (
            patch("jobindex.cli.coverage._connector_exists", return_value=True),
            patch("jobindex.cli.coverage._has_test_file", return_value=True),
            patch("jobindex.cli.coverage._has_test_fixtures", return_value=True),
        ):
            coverage = compute_coverage_state(self.platform, db_evidence)
        assert coverage.state == CoverageState.VERIFIED
        assert coverage.reason == "live jobs indexed from tenant-x (fetch_run run-1)"
        assert coverage.evidence.verified is True
        assert coverage.evidence.verified_tenants == 2
        assert coverage.evidence.last_success_at == "2024-01-01T12:00:00Z"
        assert coverage.evidence.sample_job_id == "job123"
        assert coverage.evidence.open_failures == [
            "tenant-y: health=open, consecutive_failures=3"
        ]

    def test_verified_priority_over_tested(self):
        db_evidence = {"verified": True, "verified_reason": "verified", "verified_tenants": 1}
        ci = {"passed": 4, "failed": 0}
        with (
            patch("jobindex.cli.coverage._connector_exists", return_value=True),
            patch("jobindex.cli.coverage._has_test_file", return_value=True),
            patch("jobindex.cli.coverage._has_test_fixtures", return_value=True),
        ):
            coverage = compute_coverage_state(self.platform, db_evidence, ci)
        assert coverage.state == CoverageState.VERIFIED
        assert coverage.evidence.verified is True
        assert coverage.evidence.tested is True
        assert coverage.evidence.implemented is True

    def test_unverified_db_failure_evidence_stays_outstanding(self):
        db_evidence = {
            "verified": False,
            "open_failures": ["tenant-y: health=open, consecutive_failures=3"],
        }
        with patch("jobindex.cli.coverage._connector_exists", return_value=False):
            coverage = compute_coverage_state(self.platform, db_evidence)
        assert coverage.state == CoverageState.OUTSTANDING
        assert coverage.evidence.open_failures == [
            "tenant-y: health=open, consecutive_failures=3"
        ]

    def test_outstanding_uses_inventory_reason_when_present(self):
        platform = make_platform(coverage_reason="blocked by vendor; revisit 2026-12-01")
        with patch("jobindex.cli.coverage._connector_exists", return_value=False):
            coverage = compute_coverage_state(platform)
        assert coverage.reason == "blocked by vendor; revisit 2026-12-01"

    def test_outstanding_reason_is_never_empty(self):
        platform = make_platform(coverage_reason="")
        with patch("jobindex.cli.coverage._connector_exists", return_value=False):
            coverage = compute_coverage_state(platform)
        assert coverage.reason.strip()


class TestComputeAllCoverage:
    def test_compute_all_preserves_inventory_order(self):
        platforms = list(PLATFORMS)[:5]
        coverage = compute_all_coverage(platforms)
        assert len(coverage) == 5
        assert [item.platform.key for item in coverage] == [row.key for row in platforms]

    def test_compute_all_applies_evidence_maps(self):
        platforms = list(PLATFORMS)[:2]
        db_evidence = {
            platforms[0].key: {
                "verified": True,
                "verified_reason": "live jobs indexed from tenant-a",
                "verified_tenants": 1,
                "last_success_at": "2024-01-01T00:00:00Z",
                "sample_job_id": "job-1",
                "open_failures": [],
            }
        }
        ci_evidence = {platforms[1].key: {"passed": 1, "failed": 0}}
        coverage = compute_all_coverage(platforms, db_evidence, ci_evidence)
        assert coverage[0].state == CoverageState.VERIFIED
        # No registration in the test registry, so CI alone cannot promote.
        assert coverage[1].state == CoverageState.OUTSTANDING


class TestGenerateMarkdownReport:
    def setup_method(self):
        self.platform = make_platform()

    def test_basic_report(self):
        report = generate_markdown_report(
            [coverage_for(self.platform, CoverageState.OUTSTANDING, reason="blocked")]
        )
        assert "# Connector Coverage Report" in report
        assert "Test Platform" in report
        assert "OUTSTANDING" in report
        assert "blocked" in report
        assert "Data as of: no successful live fetch recorded" in report

    def test_report_with_counts(self):
        coverage = [
            coverage_for(
                make_platform(key=f"p{i}", display_name=f"Platform {i}"),
                state,
            )
            for i, state in enumerate(
                [
                    CoverageState.OUTSTANDING,
                    CoverageState.IMPLEMENTED,
                    CoverageState.TESTED,
                    CoverageState.VERIFIED,
                ]
            )
        ]
        report = generate_markdown_report(coverage)
        assert "**Total platforms**: 4" in report
        assert "**Outstanding**: 1 / 4" in report
        assert "**Implemented**: 1 / 4" in report
        assert "**Tested**: 1 / 4" in report
        assert "**Verified**: 1 / 4" in report

    def test_wave_filter(self):
        coverage = [
            coverage_for(
                make_platform(
                    key=f"p{i}",
                    display_name=f"Platform {i}",
                    wave="S7" if i < 2 else "S8",
                ),
                CoverageState.OUTSTANDING,
            )
            for i in range(4)
        ]
        report = generate_markdown_report(coverage, wave_filter="S7")
        assert "Filtered by wave: S7" in report
        assert "Platform 0" in report
        assert "Platform 1" in report
        assert "Platform 2" not in report
        assert "Platform 3" not in report

    def test_wave_grouping(self):
        coverage = [
            coverage_for(
                make_platform(
                    key=f"p{i}",
                    display_name=f"Platform {i}",
                    wave="S7" if i < 2 else "S8",
                ),
                CoverageState.OUTSTANDING,
            )
            for i in range(4)
        ]
        report = generate_markdown_report(coverage)
        assert "## Wave S7" in report
        assert "## Wave S8" in report

    def test_deterministic_output(self):
        coverage = [coverage_for(self.platform, CoverageState.OUTSTANDING)]
        assert generate_markdown_report(coverage) == generate_markdown_report(coverage)

    def test_unknown_wave_raises(self):
        with pytest.raises(ValueError, match="unknown wave"):
            generate_markdown_report([], wave_filter="S99")


class TestGenerateJsonReport:
    def setup_method(self):
        self.platform = make_platform()

    def test_basic_json_report(self):
        report = generate_json_report(
            [coverage_for(self.platform, CoverageState.OUTSTANDING)]
        )
        assert report["schema_version"] == "1.0"
        assert report["generated_at"] is None
        assert report["wave"] is None
        assert report["summary"]["total"] == 1
        assert report["summary"]["by_state"]["outstanding"] == 1
        assert report["summary"]["by_state"]["verified"] == 0
        assert len(report["platforms"]) == 1
        entry = report["platforms"][0]
        assert entry["key"] == "testplatform"
        assert entry["display_name"] == "Test Platform"
        assert entry["coverage_state"] == "outstanding"

    def test_json_with_verified(self):
        evidence = CoverageEvidence(
            verified=True,
            verified_tenants=2,
            last_success_at="2024-01-01T12:00:00Z",
            sample_job_id="job123",
            open_failures=["failure1"],
        )
        report = generate_json_report(
            [
                coverage_for(
                    self.platform,
                    CoverageState.VERIFIED,
                    reason="verified",
                    evidence=evidence,
                )
            ]
        )
        entry = report["platforms"][0]
        assert entry["coverage_state"] == "verified"
        assert entry["verified_tenants"] == 2
        assert entry["last_success_at"] == "2024-01-01T12:00:00Z"
        assert entry["sample_job_id"] == "job123"
        assert entry["open_failures"] == ["failure1"]

    def test_json_wave_filter(self):
        coverage = [
            coverage_for(
                make_platform(key="a", display_name="A", wave="S7"),
                CoverageState.OUTSTANDING,
            ),
            coverage_for(
                make_platform(key="b", display_name="B", wave="S8"),
                CoverageState.OUTSTANDING,
            ),
        ]
        report = generate_json_report(coverage, wave_filter="S8")
        assert report["wave"] == "S8"
        assert report["summary"]["total"] == 1
        assert report["platforms"][0]["key"] == "b"

    def test_json_is_deterministic(self):
        coverage = [coverage_for(self.platform, CoverageState.OUTSTANDING)]
        first = json.dumps(generate_json_report(coverage), sort_keys=True)
        second = json.dumps(generate_json_report(coverage), sort_keys=True)
        assert first == second


class TestParsePlatformsMd:
    def test_parse_simple_table(self, tmp_path):
        path = tmp_path / "platforms.md"
        path.write_text(
            "# Platform inventory\n\n"
            "| # | Platform | Family | Class | Wave | Discovery | Auth | Reqs | Risk | Coverage |\n"
            "|---|---|---|---|---|---|---|---|---|---|\n"
            "| 1 | Test Platform | ats | C1 | S7 | test discovery | none | "
            "1 | L | outstanding |\n"
            "| 2 | Another Platform | board | C4 | S12 | test discovery | none | "
            "1 | M | implemented |\n"
        )
        header, rows = _parse_platforms_md(path)
        assert header[0].startswith("| # | Platform |")
        assert len(rows) == 2
        assert rows[0][1] == "Test Platform"
        assert rows[0][9] == "outstanding"
        assert rows[1][1] == "Another Platform"
        assert rows[1][9] == "implemented"

    def test_parse_multiple_inventory_tables(self, tmp_path):
        path = tmp_path / "platforms.md"
        path.write_text(
            "| # | Platform | Family | Class | Wave | Discovery | Auth | Reqs | Risk | Coverage |\n"
            "|---|---|---|---|---|---|---|---|---|---|\n"
            "| 1 | ATS One | ats | C1 | S7 | d | none | 1 | L | outstanding |\n"
            "\n"
            "| # | Platform | Family | Class | Wave | Discovery | Auth | Reqs | Risk | Coverage |\n"
            "|---|---|---|---|---|---|---|---|---|---|\n"
            "| 2 | Board One | board | C4 | S12 | d | none | 1 | M | outstanding |\n"
        )
        _, rows = _parse_platforms_md(path)
        assert [row[1] for row in rows] == ["ATS One", "Board One"]

    def test_parse_empty_table(self, tmp_path):
        path = tmp_path / "platforms.md"
        path.write_text(
            "| # | Platform | Family | Class | Wave | Discovery | Auth | Reqs | Risk | Coverage |\n"
            "|---|---|---|---|---|---|---|---|---|---|\n"
        )
        _, rows = _parse_platforms_md(path)
        assert rows == []

    def test_parse_escaped_pipe(self, tmp_path):
        path = tmp_path / "platforms.md"
        path.write_text(
            "| 1 | Test Platform | ats | C1 | S7 | a \\| b | none | 1 | L | outstanding |\n"
        )
        _, rows = _parse_platforms_md(path)
        assert len(rows) == 1
        assert rows[0][1] == "Test Platform"

    def test_parse_ignores_non_inventory_rows(self, tmp_path):
        path = tmp_path / "platforms.md"
        path.write_text(
            "| # | Platform | Family | Class | Wave | Discovery | Auth | Reqs | Risk | Coverage |\n"
            "|---|---|---|---|---|---|---|---|---|---|\n"
            "| 1 | Too | Few | Cells |\n"
            "| 2 | Test Platform | ats | C1 | S7 | d | none | 1 | L | outstanding |\n"
        )
        _, rows = _parse_platforms_md(path)
        assert [row[1] for row in rows] == ["Test Platform"]


class TestRegeneratePlatformsMd:
    def test_regenerate_updates_only_coverage_cells(self, tmp_path):
        path = tmp_path / "platforms.md"
        content = (
            "# Platform inventory\n"
            "\n"
            "| # | Platform | Family | Class | Wave | Discovery | Auth | Reqs | Risk | Coverage |\n"
            "|---|---|---|---|---|---|---|---|---|---|\n"
            "| 1 | Test Platform | ats | C1 | S7 | test | none | 1 | L | outstanding |\n"
            "| 2 | Another Platform | board | C4 | S12 | test | none | 1 | M | implemented |\n"
        )
        path.write_text(content, encoding="utf-8", newline="")

        coverage = [
            coverage_for(make_platform(), CoverageState.VERIFIED),
            coverage_for(
                make_platform(
                    key="anotherplatform",
                    display_name="Another Platform",
                    family="board",
                    platform_class="C4",
                    wave="S12",
                ),
                CoverageState.TESTED,
            ),
        ]

        new_content, changed = regenerate_platforms_md(path, coverage)
        assert changed is True
        assert (
            "| 1 | Test Platform | ats | C1 | S7 | test | none | 1 | L | verified |"
        ) in new_content
        assert (
            "| 2 | Another Platform | board | C4 | S12 | test | none | 1 | M | tested |"
        ) in new_content
        assert (
            new_content.replace("verified", "outstanding").replace("tested", "implemented")
            == content
        )
        assert path.read_text(encoding="utf-8") == new_content

    def test_regenerate_preserves_irregular_spacing_and_crlf(self, tmp_path):
        path = tmp_path / "platforms.md"
        content = (
            "| 1 | Test Platform | ats | C1 | S7 | test discovery | none | "
            "1+pages+N | L |  outstanding  |\r\n"
        )
        path.write_bytes(content.encode("utf-8"))

        new_content, changed = regenerate_platforms_md(
            path,
            [coverage_for(make_platform(), CoverageState.VERIFIED)],
        )
        assert changed is True
        assert new_content == (
            "| 1 | Test Platform | ats | C1 | S7 | test discovery | none | "
            "1+pages+N | L |  verified  |\r\n"
        )
        assert path.read_bytes() == new_content.encode("utf-8")

    def test_dry_run_does_not_write(self, tmp_path):
        path = tmp_path / "platforms.md"
        content = "| 1 | Test Platform | ats | C1 | S7 | test | none | 1 | L | outstanding |"
        path.write_text(content, encoding="utf-8")

        new_content, changed = regenerate_platforms_md(
            path,
            [coverage_for(make_platform(), CoverageState.VERIFIED)],
            dry_run=True,
        )
        assert changed is True
        assert "| verified |" in new_content
        assert path.read_text(encoding="utf-8") == content

    def test_no_changes_reports_false(self, tmp_path):
        path = tmp_path / "platforms.md"
        content = "| 1 | Test Platform | ats | C1 | S7 | test | none | 1 | L | outstanding |\n"
        path.write_text(content, encoding="utf-8", newline="")

        new_content, changed = regenerate_platforms_md(
            path,
            [coverage_for(make_platform(), CoverageState.OUTSTANDING)],
        )
        assert changed is False
        assert new_content == content

    def test_missing_coverage_raises(self, tmp_path):
        path = tmp_path / "platforms.md"
        path.write_text(
            "| 1 | Test Platform | ats | C1 | S7 | test | none | 1 | L | outstanding |\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="no coverage supplied"):
            regenerate_platforms_md(path, [])

    def test_unknown_coverage_raises(self, tmp_path):
        path = tmp_path / "platforms.md"
        path.write_text(
            "| 1 | Test Platform | ats | C1 | S7 | test | none | 1 | L | outstanding |\n",
            encoding="utf-8",
        )
        extra = coverage_for(
            make_platform(key="extra", display_name="Extra"),
            CoverageState.OUTSTANDING,
        )
        with pytest.raises(ValueError, match="unknown platform"):
            regenerate_platforms_md(
                path,
                [coverage_for(make_platform(), CoverageState.OUTSTANDING), extra],
            )

    def test_duplicate_coverage_raises(self, tmp_path):
        path = tmp_path / "platforms.md"
        path.write_text(
            "| 1 | Test Platform | ats | C1 | S7 | test | none | 1 | L | outstanding |\n",
            encoding="utf-8",
        )
        item = coverage_for(make_platform(), CoverageState.OUTSTANDING)
        with pytest.raises(ValueError, match="duplicate coverage"):
            regenerate_platforms_md(path, [item, item])

    def test_real_inventory_is_byte_identical_when_nothing_changes(self, tmp_path):
        rows = load_platform_rows()
        coverage = [
            coverage_for(row, CoverageState.OUTSTANDING)
            for row in rows
        ]
        original = Path("docs/platforms.md").read_text(encoding="utf-8")
        new_content, changed = regenerate_platforms_md(
            Path("docs/platforms.md"),
            coverage,
            dry_run=True,
        )
        assert changed is False
        assert new_content == original


def make_ci_payload(platforms: dict) -> dict:
    """Create a valid CI evidence payload with required provenance."""
    return {
        "schema_version": "2.0",
        "provenance": {
            "repository": "test/repo",
            "commit_sha": "abc123def456",
            "workflow_run_id": 123456789,
            "workflow_name": "CI",
            "test_suite": "connectors",
        },
        "platforms": platforms,
    }


class TestLoadCiEvidence:
    def write(self, tmp_path: Path, payload: object) -> Path:
        path = tmp_path / "ci.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_none_returns_empty(self):
        assert load_ci_evidence(None) == {}

    def test_valid_evidence(self, tmp_path):
        path = self.write(
            tmp_path,
            make_ci_payload({
                "greenhouse": {
                    "passed": 12,
                    "failed": 0,
                    "skipped": 1,
                    "report": "https://github.com/test/repo/actions/runs/123456789",
                }
            }),
        )
        evidence = load_ci_evidence(path)
        assert evidence == {
            "greenhouse": {
                "passed": 12,
                "failed": 0,
                "skipped": 1,
                "report": "https://github.com/test/repo/actions/runs/123456789",
            }
        }

    def test_unknown_key_raises(self, tmp_path):
        path = self.write(
            tmp_path,
            make_ci_payload({"not-a-platform": {"passed": 1}}),
        )
        with pytest.raises(ValueError, match="unknown platform key"):
            load_ci_evidence(path)

    def test_wrong_schema_version_raises(self, tmp_path):
        path = self.write(tmp_path, {"schema_version": "1.0", "platforms": {}})
        with pytest.raises(ValueError, match="schema_version"):
            load_ci_evidence(path)

    def test_missing_provenance_raises(self, tmp_path):
        path = self.write(tmp_path, {"schema_version": "2.0", "platforms": {}})
        with pytest.raises(ValueError, match="provenance"):
            load_ci_evidence(path)

    def test_incomplete_provenance_raises(self, tmp_path):
        path = self.write(
            tmp_path,
            {
                "schema_version": "2.0",
                "provenance": {"repository": "test/repo"},
                "platforms": {},
            },
        )
        with pytest.raises(ValueError, match=r"provenance\.commit_sha"):
            load_ci_evidence(path)

    def test_invalid_workflow_run_id_raises(self, tmp_path):
        path = self.write(
            tmp_path,
            {
                "schema_version": "2.0",
                "provenance": {
                    "repository": "test/repo",
                    "commit_sha": "abc123",
                    "workflow_run_id": "not-an-int",
                    "workflow_name": "CI",
                    "test_suite": "connectors",
                },
                "platforms": {},
            },
        )
        with pytest.raises(ValueError, match="workflow_run_id"):
            load_ci_evidence(path)

    def test_missing_platforms_raises(self, tmp_path):
        path = self.write(tmp_path, {"schema_version": "2.0", "provenance": {
            "repository": "test/repo", "commit_sha": "abc", "workflow_run_id": 1,
            "workflow_name": "CI", "test_suite": "connectors"
        }})
        with pytest.raises(ValueError, match="platforms"):
            load_ci_evidence(path)

    def test_negative_count_raises(self, tmp_path):
        path = self.write(
            tmp_path,
            make_ci_payload({"greenhouse": {"passed": -1}}),
        )
        with pytest.raises(ValueError, match="non-negative integer"):
            load_ci_evidence(path)

    def test_bool_count_raises(self, tmp_path):
        path = self.write(
            tmp_path,
            make_ci_payload({"greenhouse": {"passed": True}}),
        )
        with pytest.raises(ValueError, match="non-negative integer"):
            load_ci_evidence(path)

    def test_invalid_json_raises(self, tmp_path):
        path = tmp_path / "ci.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError, match="not valid JSON"):
            load_ci_evidence(path)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(ValueError, match="cannot read"):
            load_ci_evidence(tmp_path / "missing.json")

    def test_failed_tests_do_not_pass(self, tmp_path):
        """CI evidence with failed tests does not promote to TESTED."""
        from jobindex.cli.coverage import _ci_passed

        ci = {"passed": 10, "failed": 1, "skipped": 0}
        assert _ci_passed(ci) is False

    def test_zero_passed_does_not_pass(self, tmp_path):
        """CI evidence with zero passed tests does not promote to TESTED."""
        from jobindex.cli.coverage import _ci_passed

        ci = {"passed": 0, "failed": 0, "skipped": 5}
        assert _ci_passed(ci) is False


class TestCoverageCli:
    def run(self, argv: list[str]) -> int:
        from jobindex.cli.coverage import cmd_coverage

        parser = build_coverage_parser_standalone()
        return cmd_coverage(parser.parse_args(["coverage", *argv]))

    def test_dry_run_writes_nothing(self, tmp_path, capsys):
        output = tmp_path / "coverage.md"
        report = tmp_path / "coverage.json"
        code = self.run(
            [
                "--no-db",
                "--output",
                str(output),
                "--json",
                str(report),
                "--dry-run",
            ]
        )
        assert code == 0
        assert not output.exists()
        assert not report.exists()
        captured = capsys.readouterr().out
        assert "# Connector Coverage Report" in captured
        assert "proposed" in captured

    def test_writes_markdown_and_json(self, tmp_path):
        output = tmp_path / "coverage.md"
        report = tmp_path / "coverage.json"
        code = self.run(["--no-db", "--output", str(output), "--json", str(report)])
        assert code == 0
        assert output.read_text(encoding="utf-8").startswith("# Connector Coverage Report")
        payload = json.loads(report.read_text(encoding="utf-8"))
        assert payload["schema_version"] == "1.0"
        assert payload["summary"]["total"] == 78
        assert payload["summary"]["by_state"]["outstanding"] == 78

    def test_wave_without_output_prints_and_does_not_write(self, tmp_path, monkeypatch, capsys):
        default_path = tmp_path / "default.md"
        monkeypatch.setattr("jobindex.cli.coverage.DEFAULT_REPORT_PATH", default_path)
        code = self.run(["--no-db", "--wave", "S3"])
        assert code == 0
        assert not default_path.exists()
        captured = capsys.readouterr().out
        assert "Filtered by wave: S3" in captured
        assert "Greenhouse" in captured
        assert "Ashby" not in captured

    def test_update_platforms_md(self, tmp_path, monkeypatch, capsys):
        inventory = tmp_path / "platforms.md"
        rows = load_platform_rows()
        inventory.write_text(
            "\n".join(
                [
                    "# Inventory",
                    "",
                    (
                        "| # | Platform | Family | Class | Wave | Discovery | "
                        "Auth | Reqs | Risk | Coverage |"
                    ),
                    "|---|---|---|---|---|---|---|---|---|---|",
                ]
                + [
                    f"| {index} | {row.display_name} | {row.family} | "
                    f"{row.platform_class} | {row.wave} | {row.discovery} | "
                    f"{row.auth_kind} | 1 | {row.risk} | outstanding |"
                    for index, row in enumerate(rows, start=1)
                ]
            )
            + "\n",
            encoding="utf-8",
            newline="",
        )
        monkeypatch.setattr("jobindex.cli.coverage.INVENTORY_PATH", inventory)
        # This test runs without database, so --update-platforms-md should fail
        code = self.run(["--no-db", "--update-platforms-md"])
        assert code == 3
        assert "cannot use --no-db" in capsys.readouterr().err

    def test_invalid_test_evidence_returns_error(self, tmp_path, capsys):
        bad = tmp_path / "bad.json"
        bad.write_text("{}", encoding="utf-8")
        code = self.run(["--no-db", "--test-evidence", str(bad), "--wave", "S3"])
        assert code == 2
        assert "schema_version" in capsys.readouterr().err

    def test_valid_test_evidence_promotes_to_tested(self, tmp_path, monkeypatch):
        """Valid CI evidence with passing tests promotes platform to TESTED."""
        # Mock the registry to have greenhouse registered with fixture-backed test
        from jobindex.cli.coverage import get_registry
        from jobindex.connectors.base import (
            Completeness,
            ListingPage,
            RawPosting,
            Tenant,
        )

        class MockConnector:
            platform_key = "greenhouse"
            transport = "API"
            completeness = Completeness.FULL
            async def list_page(self, tenant: Tenant, cursor: str | None = None):
                return ListingPage(postings=())
            async def fetch_detail(self, tenant: Tenant, posting: RawPosting):
                return posting

        registry = get_registry()
        registry._factories.clear()
        registry.register("greenhouse", lambda ctx: MockConnector())

        # Mock test file and fixtures existence
        monkeypatch.setattr("jobindex.cli.coverage._has_test_file", lambda key: True)
        monkeypatch.setattr("jobindex.cli.coverage._has_test_fixtures", lambda key: True)

        # Create valid CI evidence
        ci_file = tmp_path / "ci.json"
        ci_file.write_text(
            json.dumps(make_ci_payload({
                "greenhouse": {"passed": 5, "failed": 0, "skipped": 0, "report": "https://ci/1"}
            })),
            encoding="utf-8",
        )
        output = tmp_path / "coverage.md"
        args = ["--no-db", "--test-evidence", str(ci_file), "--output", str(output), "--wave", "S3"]
        code = self.run(args)
        assert code == 0
        content = output.read_text(encoding="utf-8")
        assert "TESTED" in content
        assert "Greenhouse" in content

    def test_update_platforms_md_requires_db(self, tmp_path, monkeypatch, capsys):
        """--update-platforms-md without database fails with non-zero exit."""
        inventory = tmp_path / "platforms.md"
        rows = load_platform_rows()
        inventory.write_text(
            "\n".join(
                [
                    "# Inventory",
                    "",
                    (
                        "| # | Platform | Family | Class | Wave | Discovery | "
                        "Auth | Reqs | Risk | Coverage |"
                    ),
                    "|---|---|---|---|---|---|---|---|---|---|",
                ]
                + [
                    f"| {index} | {row.display_name} | {row.family} | "
                    f"{row.platform_class} | {row.wave} | {row.discovery} | "
                    f"{row.auth_kind} | 1 | {row.risk} | outstanding |"
                    for index, row in enumerate(rows, start=1)
                ]
            )
            + "\n",
            encoding="utf-8",
            newline="",
        )
        monkeypatch.setattr("jobindex.cli.coverage.INVENTORY_PATH", inventory)
        original_content = inventory.read_text(encoding="utf-8")

        # Without --no-db, but database unavailable, should fail
        # We can't easily mock the database connection here, so we test with --no-db
        # which should explicitly fail when combined with --update-platforms-md
        code = self.run(["--no-db", "--update-platforms-md"])
        assert code == 3
        assert "cannot use --no-db" in capsys.readouterr().err
        # Verify inventory was not modified
        assert inventory.read_text(encoding="utf-8") == original_content

    def test_no_db_allows_report_generation(self, tmp_path, monkeypatch):
        """--no-db allows report generation but not inventory mutation."""
        inventory = tmp_path / "platforms.md"
        rows = load_platform_rows()
        inventory.write_text(
            "\n".join(
                [
                    "# Inventory",
                    "",
                    (
                        "| # | Platform | Family | Class | Wave | Discovery | "
                        "Auth | Reqs | Risk | Coverage |"
                    ),
                    "|---|---|---|---|---|---|---|---|---|---|",
                ]
                + [
                    f"| {index} | {row.display_name} | {row.family} | "
                    f"{row.platform_class} | {row.wave} | {row.discovery} | "
                    f"{row.auth_kind} | 1 | {row.risk} | outstanding |"
                    for index, row in enumerate(rows, start=1)
                ]
            )
            + "\n",
            encoding="utf-8",
            newline="",
        )
        monkeypatch.setattr("jobindex.cli.coverage.INVENTORY_PATH", inventory)
        original_content = inventory.read_text(encoding="utf-8")

        output = tmp_path / "coverage.md"
        code = self.run(["--no-db", "--output", str(output)])
        assert code == 0
        assert output.exists()
        # Inventory should not be modified
        assert inventory.read_text(encoding="utf-8") == original_content

    def test_dry_run_does_not_modify_files(self, tmp_path):
        """--dry-run never modifies any files."""
        output = tmp_path / "coverage.md"
        report = tmp_path / "coverage.json"
        code = self.run(
            [
                "--no-db",
                "--output",
                str(output),
                "--json",
                str(report),
                "--dry-run",
            ]
        )
        assert code == 0
        assert not output.exists()
        assert not report.exists()


def build_coverage_parser_standalone():
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    build_coverage_parser(sub)
    return parser


class TestCliSurface:
    def test_platforms_command_still_lists_78(self, capsys):
        from jobindex.cli import main

        assert main(["platforms"]) == 0
        captured = capsys.readouterr().out
        assert "78 platforms" in captured
        assert "greenhouse" in captured

    def test_coverage_subcommand_is_registered(self):
        from jobindex.cli import build_parser

        args = build_parser().parse_args(["coverage", "--no-db", "--wave", "S3"])
        assert args.command == "coverage"
        assert args.wave == "S3"
        assert callable(args.func)
