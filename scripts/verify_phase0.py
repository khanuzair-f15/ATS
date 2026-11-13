#!/usr/bin/env python3
"""Phase 0 gate: prove the platform inventory matches the brief's frozen manifest.

Checks
------
1. docs/brief-platforms.txt parses to exactly 58 ATS + 20 BOARD = 78, no duplicates.
2. docs/platforms.md has 78 numbered data rows, in the same order and with the same
   exact names as the manifest (both directions, so nothing was renamed or substituted).
3. Every row has one class (C1-C4), one wave (S3/S7/S8/S10/S11/S12/S13), and one
   coverage state (implemented/tested/verified/outstanding).
4. Phase 0 has no runtime, so every coverage state is `outstanding`.
5. Wave and class counts match the approved distribution and sum to 78.
6. The ownership table in docs/platforms.md agrees with the data rows and sums to 78.
7. Every step `Deps:` target in the plan exists, and every `docs/schema.md` section
   reference resolves.

Exit codes: 0 = pass, 1 = violations, 2 = missing manifest.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "docs" / "brief-platforms.txt"
INVENTORY = ROOT / "docs" / "platforms.md"
PLAN = ROOT / "plans" / "jobindex-blueprint.md"
SCHEMA = ROOT / "docs" / "schema.md"

EXPECTED_FAMILY = {"ATS": 58, "BOARD": 20}
EXPECTED_TOTAL = 78
EXPECTED_CLASS = {"C1": 11, "C2": 21, "C3": 26, "C4": 20}
EXPECTED_WAVE = {"S3": 1, "S7": 10, "S8": 6, "S10": 8, "S11": 7, "S12": 20, "S13": 26}
COVERAGE_STATES = {"implemented", "tested", "verified", "outstanding"}
CLASSES = {"C1", "C2", "C3", "C4"}

violations: list[str] = []
notes: list[str] = []


def fail(message: str) -> None:
    violations.append(message)


def parse_manifest() -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for lineno, raw in enumerate(MANIFEST.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) != 2:
            fail(f"manifest line {lineno}: expected '<family>\\t<name>', got {raw!r}")
            continue
        family, name = parts[0].strip(), parts[1].strip()
        if family not in EXPECTED_FAMILY:
            fail(f"manifest line {lineno}: unknown family {family!r}")
        if not name:
            fail(f"manifest line {lineno}: empty platform name")
        rows.append((family, name))
    return rows


def split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_inventory() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for lineno, raw in enumerate(INVENTORY.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = split_row(line)
        if not cells or not cells[0].isdigit():
            continue
        if len(cells) != 10:
            fail(
                f"inventory line {lineno}: expected 10 columns, got {len(cells)} "
                f"({cells[0]}: {cells[1] if len(cells) > 1 else '?'})"
            )
            continue
        number, name, family, klass, wave, discovery, auth, reqs, risk, coverage = cells
        rows.append(
            {
                "number": number,
                "name": name,
                "family": family,
                "class": klass,
                "wave": wave,
                "discovery": discovery,
                "auth": auth,
                "reqs": reqs,
                "risk": risk,
                "coverage": coverage,
                "line": str(lineno),
            }
        )
    return rows


def parse_ownership(inventory_text: str) -> dict[str, int]:
    """Read the §1a table: | Wave | Step | Rows | Count |."""
    counts: dict[str, int] = {}
    for raw in inventory_text.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = split_row(line)
        if len(cells) != 4:
            continue
        step, count = cells[1], cells[3]
        if re.fullmatch(r"S\d+", step) and count.isdigit():
            counts[step] = counts.get(step, 0) + int(count)
    return counts


def parse_schema_sections() -> set[int]:
    text = SCHEMA.read_text(encoding="utf-8")
    return {int(m.group(1)) for m in re.finditer(r"^## (\d+)\.", text, re.MULTILINE)}


def check_schema_refs(sections: set[int]) -> None:
    files = [ROOT / "README.md", PLAN, SCHEMA]
    files += sorted((ROOT / "docs").glob("*.md"))
    files += sorted((ROOT / "docs" / "adr").glob("*.md"))
    files += sorted((ROOT / "plans").glob("*.md"))
    pattern = re.compile(r"docs/schema\.md\s*§(\d+)(?:\s*[–-]\s*§?(\d+))?")
    for path in dict.fromkeys(files):
        if not path.exists():
            continue
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else start
            for section in range(start, end + 1):
                if section not in sections:
                    fail(
                        f"{path.relative_to(ROOT)}: schema reference §{section} "
                        f"does not resolve"
                    )


def check_steps() -> None:
    text = PLAN.read_text(encoding="utf-8")
    steps = set(re.findall(r"^### (S\d+)\b", text, re.MULTILINE))
    if not steps:
        fail("plan: no step headings found")
        return
    blocks = re.split(r"^### (S\d+)\b", text, flags=re.MULTILINE)
    # blocks = [preamble, 'S0', body, 'S1', body, ...]
    for index in range(1, len(blocks), 2):
        step = blocks[index]
        body = blocks[index + 1]
        deps_match = re.search(r"\*\*Deps:\*\*\s*([^\n·]*)", body)
        if not deps_match:
            if step != "S0":
                fail(f"plan: step {step} has no **Deps:** line")
            continue
        for dep in re.findall(r"S\d+", deps_match.group(1)):
            if dep not in steps:
                fail(f"plan: step {step} depends on unknown step {dep}")
    notes.append(f"plan: {len(steps)} steps, dependency targets resolve")


def main() -> int:
    if not MANIFEST.exists():
        print(f"MISSING MANIFEST: {MANIFEST}", file=sys.stderr)
        return 2

    manifest = parse_manifest()
    family_counts = Counter(family for family, _ in manifest)
    for family, expected in EXPECTED_FAMILY.items():
        if family_counts.get(family) != expected:
            fail(f"manifest: {family} count {family_counts.get(family)} != {expected}")
    if len(manifest) != EXPECTED_TOTAL:
        fail(f"manifest: total {len(manifest)} != {EXPECTED_TOTAL}")
    if len({name for _, name in manifest}) != len(manifest):
        fail("manifest: duplicate platform name")

    inventory = parse_inventory()
    if len(inventory) != EXPECTED_TOTAL:
        fail(f"inventory: {len(inventory)} data rows != {EXPECTED_TOTAL}")

    numbers = [int(row["number"]) for row in inventory]
    if numbers != list(range(1, EXPECTED_TOTAL + 1)):
        fail("inventory: row numbers are not 1..78 in order")

    manifest_pairs = [(family.lower(), name) for family, name in manifest]
    inventory_pairs = [(row["family"].lower(), row["name"]) for row in inventory]
    if manifest_pairs != inventory_pairs:
        for index, (want, got) in enumerate(zip(manifest_pairs, inventory_pairs), 1):
            if want != got:
                fail(f"inventory row {index}: manifest {want!r} != inventory {got!r}")
        if len(manifest_pairs) != len(inventory_pairs):
            fail(
                "inventory: row count differs from manifest "
                f"({len(inventory_pairs)} vs {len(manifest_pairs)})"
            )

    class_counts: Counter[str] = Counter()
    wave_counts: Counter[str] = Counter()
    coverage_counts: Counter[str] = Counter()
    for row in inventory:
        label = f"row {row['number']} ({row['name']})"
        if row["class"] not in CLASSES:
            fail(f"{label}: invalid class {row['class']!r}")
        if row["wave"] not in EXPECTED_WAVE:
            fail(f"{label}: invalid wave {row['wave']!r}")
        if row["coverage"] not in COVERAGE_STATES:
            fail(f"{label}: invalid coverage state {row['coverage']!r}")
        if row["coverage"] != "outstanding":
            fail(f"{label}: Phase 0 has no runtime, coverage must be 'outstanding'")
        if "*" in row["class"] or "*" in row["wave"]:
            fail(f"{label}: class/wave must be machine-readable (no markdown)")
        if not row["discovery"]:
            fail(f"{label}: empty discovery/detail cell")
        class_counts[row["class"]] += 1
        wave_counts[row["wave"]] += 1
        coverage_counts[row["coverage"]] += 1

    if dict(class_counts) != EXPECTED_CLASS:
        fail(f"inventory: class counts {dict(class_counts)} != {EXPECTED_CLASS}")
    if dict(wave_counts) != EXPECTED_WAVE:
        fail(f"inventory: wave counts {dict(wave_counts)} != {EXPECTED_WAVE}")
    if sum(wave_counts.values()) != EXPECTED_TOTAL:
        fail(f"inventory: wave counts sum {sum(wave_counts.values())} != {EXPECTED_TOTAL}")

    ownership = parse_ownership(INVENTORY.read_text(encoding="utf-8"))
    if not ownership:
        fail("inventory: §1a ownership table not found or empty")
    else:
        if ownership != dict(wave_counts):
            fail(f"inventory: §1a counts {ownership} != data-row counts {dict(wave_counts)}")
        if sum(ownership.values()) != EXPECTED_TOTAL:
            fail(f"inventory: §1a counts sum {sum(ownership.values())} != {EXPECTED_TOTAL}")

    sections = parse_schema_sections()
    if not sections:
        fail("schema: no numbered sections found")
    else:
        check_schema_refs(sections)

    check_steps()

    print("Phase 0 gate")
    print(f"  manifest : ATS={family_counts.get('ATS', 0)} "
          f"BOARD={family_counts.get('BOARD', 0)} total={len(manifest)}")
    print(f"  inventory: rows={len(inventory)} classes={dict(class_counts)}")
    print(f"  waves    : {dict(wave_counts)} sum={sum(wave_counts.values())}")
    print(f"  coverage : {dict(coverage_counts)}")
    print(f"  schema   : {len(sections)} numbered sections, references resolve")
    for note in notes:
        print(f"  note     : {note}")

    if violations:
        print(f"\nFAIL: {len(violations)} violation(s)")
        for violation in violations:
            print(f"  - {violation}")
        return 1

    print("\nPASS: inventory is brief-exact, waves and classes sum to 78, no EX rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
