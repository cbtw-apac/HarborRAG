"""Run one real connector smoke check, driven by config/env, with real parsing.

    python packages/harborrag-adapters/tests/smoke/connectors/run.py --connector jira
    python packages/harborrag-adapters/tests/smoke/connectors/run.py --connector confluence --output txt
    python packages/harborrag-adapters/tests/smoke/connectors/run.py --connector local --output md

Connectors come from `config/connectors.yaml`, parsers (Docling for PDF,
RapidOCR for images) come from `config/parsers.yaml`, and credentials come
from `env/.env.connector` / `env/.env.parser`. See connectors/README.md.

Each connector's own discover/load/parse/save logic lives in its own module
(`confluence.py`, `jira.py`, `local.py`); this script only parses CLI
arguments and dispatches to the requested one.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from bootstrap import SUPPORTED_OUTPUT_FORMATS  # noqa: E402
from confluence import run_confluence  # noqa: E402
from jira import run_jira  # noqa: E402
from local import run_local  # noqa: E402

RUNNERS = {
    "confluence": run_confluence,
    "jira": run_jira,
    "local": run_local,
}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one real connector smoke check with configured parsing.",
    )
    parser.add_argument(
        "--connector",
        required=True,
        choices=sorted(RUNNERS),
        help="Connector to exercise.",
    )
    parser.add_argument(
        "--output",
        choices=list(SUPPORTED_OUTPUT_FORMATS),
        default=None,
        help="Save parsed output as a file (txt: flat text, md: structured "
        "Markdown with a title/metadata header and attachment sections). "
        "Default: do not save.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for saved output (default: tests/smoke/connectors/output).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=3,
        help="Max records to discover and process (default: 3). Each record "
        "gets its own output file when --output is set.",
    )
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    run = RUNNERS[args.connector]
    return run(limit=args.limit, output=args.output, output_dir=args.output_dir)


if __name__ == "__main__":
    raise SystemExit(main())
