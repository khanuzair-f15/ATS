"""Phase 5A verification command (CLI entry point).

Delegates to :func:`jobindex.cloud.verification.run_verification`, which runs
all offline checks without a live database or deployed Vercel/Supabase project.

Usage:
    python scripts/verify_cloud.py        # summary text, exit 0 on success
    python scripts/verify_cloud.py --json # machine-readable JSON
"""

from __future__ import annotations

import argparse
import sys

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobindex.cloud.verification import run_verification  # noqa: E402


def build_parser():
    parser = argparse.ArgumentParser(prog="verify_cloud")
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of summary text",
    )
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    all_ok, _results = run_verification(json_output=args.json)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
