"""The platform registry, read from `docs/platforms.md`.

`docs/platforms.md` is the human-reviewable authority for the platform inventory; the
`platform` table is its machine copy. Rather than keeping a second hand-written list in
Python (which drifts), the seed migration and the tests both parse the document.

The document has no `key` column, so keys are derived deterministically:

    lower-case, drop every character outside [a-z0-9]

`Lever.co` -> `leverco`, `WP Job Manager` -> `wpjobmanager`. Keys are internal
identifiers (`source.platform_key`, CLI arguments); display names are what humans see.
`assert_unique_keys()` runs at import so a future rename that collides fails loudly
instead of silently overwriting a row.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INVENTORY_PATH = ROOT / "docs" / "platforms.md"

VALID_FAMILIES = {"ats", "board"}
VALID_CLASSES = {"C1", "C2", "C3", "C4"}
VALID_WAVES = {"S3", "S7", "S8", "S10", "S11", "S12", "S13"}
VALID_COVERAGE = {"implemented", "tested", "verified", "outstanding"}
VALID_AUTH = {"none", "token", "partner"}

_KEY_STRIP = re.compile(r"[^a-z0-9]")


def platform_key(display_name: str) -> str:
    key = _KEY_STRIP.sub("", display_name.lower())
    if not key:
        raise ValueError(f"cannot derive a platform key from {display_name!r}")
    return key


@dataclass(frozen=True, slots=True)
class PlatformRow:
    key: str
    display_name: str
    family: str
    platform_class: str
    wave: str
    auth_kind: str
    coverage_state: str
    coverage_reason: str
    discovery: str
    risk: str

    @property
    def notes(self) -> str:
        """Free-text provenance carried into `platform.notes`."""
        return f"discovery: {self.discovery} | risk: {self.risk}"


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_inventory(text: str) -> list[PlatformRow]:
    rows: list[PlatformRow] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = _cells(line)
        if not cells or not cells[0].isdigit():
            continue
        if len(cells) != 10:
            raise ValueError(f"inventory row {cells[0]}: expected 10 columns, got {len(cells)}")
        number, name, family, klass, wave, discovery, auth, _reqs, risk, coverage = cells

        family_norm = family.lower()
        if family_norm not in VALID_FAMILIES:
            raise ValueError(f"row {number} ({name}): invalid family {family!r}")
        if klass not in VALID_CLASSES:
            raise ValueError(f"row {number} ({name}): invalid class {klass!r}")
        if wave not in VALID_WAVES:
            raise ValueError(f"row {number} ({name}): invalid wave {wave!r}")
        if coverage not in VALID_COVERAGE:
            raise ValueError(f"row {number} ({name}): invalid coverage {coverage!r}")
        if auth not in VALID_AUTH:
            raise ValueError(f"row {number} ({name}): invalid auth {auth!r}")

        rows.append(
            PlatformRow(
                key=platform_key(name),
                display_name=name,
                family=family_norm,
                platform_class=klass,
                wave=wave,
                auth_kind=auth,
                coverage_state=coverage,
                # Phase 0 has no connector for any row, so the reason is uniform and
                # honest. From S2 the coverage generator supplies a specific reason.
                coverage_reason="not started: no connector yet (owner: unassigned)",
                discovery=discovery,
                risk=risk,
            )
        )
    return rows


def load_platform_rows(path: Path | None = None) -> list[PlatformRow]:
    source = path or INVENTORY_PATH
    return parse_inventory(source.read_text(encoding="utf-8"))


def assert_unique_keys(rows: list[PlatformRow]) -> None:
    seen: dict[str, str] = {}
    for row in rows:
        if row.key in seen:
            raise ValueError(
                f"platform key collision {row.key!r}: {seen[row.key]!r} vs {row.display_name!r}"
            )
        seen[row.key] = row.display_name


PLATFORMS: tuple[PlatformRow, ...] = tuple(load_platform_rows())
assert_unique_keys(list(PLATFORMS))
PLATFORMS_BY_KEY: dict[str, PlatformRow] = {row.key: row for row in PLATFORMS}
