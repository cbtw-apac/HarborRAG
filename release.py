#!/usr/bin/env python3
"""Entry point for HarborRAG's coordinated release command.

Run ``uv run python release.py --help``. The implementation lives in the
``release_support`` package; import from there rather than from this script.
"""

from release_support.cli import release

if __name__ == "__main__":
    release()
