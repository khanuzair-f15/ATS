"""`jobindex` command line.

S1 exposes only what exists: inventory inspection. The coverage generator (`coverage`),
which regenerates `docs/platforms.md`'s `Coverage` column from the database, ships in S2.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from jobindex.platforms import PLATFORMS


def _cmd_platforms(_: argparse.Namespace) -> int:
    counts = Counter(row.platform_class for row in PLATFORMS)
    waves = Counter(row.wave for row in PLATFORMS)
    print(f"{len(PLATFORMS)} platforms")
    print("  classes: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print("  waves  : " + ", ".join(f"{k}={v}" for k, v in sorted(waves.items())))
    for row in PLATFORMS:
        print(f"  {row.key:<24} {row.display_name:<24} {row.platform_class} {row.wave}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobindex")
    sub = parser.add_subparsers(dest="command", required=True)
    platforms = sub.add_parser("platforms", help="list the platform inventory")
    platforms.set_defaults(func=_cmd_platforms)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
