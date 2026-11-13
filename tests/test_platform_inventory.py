"""Platform inventory tests — offline, no database required.

These tests assert the frozen manifest (docs/brief-platforms.txt) and the
human-reviewable inventory (docs/platforms.md) agree exactly, and that the
in-code registry (jobindex.platforms) parses them correctly.

All 78 platforms must be present with:
- correct class (C1=11, C2=21, C3=26, C4=20)
- correct wave (S3=1, S7=10, S8=6, S10=8, S11=7, S12=20, S13=26)
- coverage_state = 'outstanding' (Phase 0 has no connectors)
"""

from __future__ import annotations

from collections import Counter

from jobindex.platforms import PLATFORMS, load_platform_rows

EXPECTED_TOTAL = 78
EXPECTED_CLASS = {"C1": 11, "C2": 21, "C3": 26, "C4": 20}
EXPECTED_WAVE = {"S3": 1, "S7": 10, "S8": 6, "S10": 8, "S11": 7, "S12": 20, "S13": 26}
VALID_COVERAGE = {"implemented", "tested", "verified", "outstanding"}


def test_platforms_loaded_from_docs() -> None:
    rows = load_platform_rows()
    assert len(rows) == EXPECTED_TOTAL, f"expected {EXPECTED_TOTAL} rows, got {len(rows)}"


def test_platforms_module_constant_matches() -> None:
    # PLATFORMS tuple in jobindex.platforms should match the loader
    assert len(PLATFORMS) == EXPECTED_TOTAL


def test_no_duplicate_keys() -> None:
    keys = [row.key for row in PLATFORMS]
    assert len(keys) == len(set(keys)), "duplicate platform keys detected"


def test_class_distribution() -> None:
    counts = Counter(row.platform_class for row in PLATFORMS)
    assert dict(counts) == EXPECTED_CLASS, f"class mismatch: {dict(counts)} != {EXPECTED_CLASS}"


def test_wave_distribution() -> None:
    counts = Counter(row.wave for row in PLATFORMS)
    assert dict(counts) == EXPECTED_WAVE, f"wave mismatch: {dict(counts)} != {EXPECTED_WAVE}"


def test_all_coverage_outstanding_in_phase0() -> None:
    for row in PLATFORMS:
        assert row.coverage_state in VALID_COVERAGE
        assert (
            row.coverage_state == "outstanding"
        ), (
            f"{row.display_name}: coverage_state={row.coverage_state!r}, "
            "expected 'outstanding' in Phase 0"
        )


def test_coverage_reason_present_when_outstanding() -> None:
    for row in PLATFORMS:
        if row.coverage_state == "outstanding":
            assert (
                row.coverage_reason
            ), f"{row.display_name}: coverage_reason required when outstanding"


def test_family_values() -> None:
    families = {row.family for row in PLATFORMS}
    assert families == {"ats", "board"}, f"unexpected families: {families}"


def test_auth_kind_values() -> None:
    auths = {row.auth_kind for row in PLATFORMS}
    assert auths.issubset({"none", "token", "partner"}), f"unexpected auth_kind: {auths}"


def test_discovery_and_risk_populated() -> None:
    for row in PLATFORMS:
        assert row.discovery, f"{row.display_name}: discovery is empty"
        assert row.risk, f"{row.display_name}: risk is empty"


def test_notes_field_contains_provenance() -> None:
    for row in PLATFORMS:
        notes = row.notes
        assert "discovery:" in notes
        assert "risk:" in notes