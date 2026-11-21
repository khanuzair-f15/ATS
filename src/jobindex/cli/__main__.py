"""Entry point for `python -m jobindex.cli`."""

import sys

from jobindex.cli.main import main

if __name__ == "__main__":
    sys.exit(main())