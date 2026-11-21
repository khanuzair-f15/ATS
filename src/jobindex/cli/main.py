"""`jobindex` command line.

S1 exposes only what exists: inventory inspection. The coverage generator (`coverage`),
which regenerates `docs/platforms.md`'s `Coverage` column from the database, ships in S2.
The `cloud` subcommand (Phase 5A prototype) exposes the offline cloud-execution
verification command.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from jobindex.cli.coverage import build_coverage_parser
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


def _cmd_cloud_verify(ns: argparse.Namespace) -> int:
    from jobindex.cloud.verification import run_verification

    _ = ns
    all_ok, _results = run_verification(json_output=False)
    return 0 if all_ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobindex")
    sub = parser.add_subparsers(dest="command", required=True)
    platforms = sub.add_parser("platforms", help="list the platform inventory")
    platforms.set_defaults(func=_cmd_platforms)

    # Add coverage subcommand
    build_coverage_parser(sub)

    # Phase 5A cloud verification command
    cloud = sub.add_parser("cloud", help="Phase 5A cloud-execution verification")
    cloud_sub = cloud.add_subparsers(dest="cloud_command", required=True)
    verify = cloud_sub.add_parser("verify", help="verify the cloud execution prototype offline")
    verify.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of summary text",
    )
    verify.set_defaults(func=_cmd_cloud_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())