"""Run one real connector smoke check, driven by config/env, with real parsing.

    python packages/harborrag-adapters/tests/connectors/smoke/run.py --connector jira-main
    python packages/harborrag-adapters/tests/connectors/smoke/run.py --connector confluence-main --output txt
    python packages/harborrag-adapters/tests/connectors/smoke/run.py --connector harborrag-workspace --output md

Connectors come from `config/connectors.yaml`, parsers (Docling for PDF,
RapidOCR for images) come from `config/parsers.yaml`, and credentials come
from `env/.env.connector` / `env/.env.parser`. See connectors/README.md.

Each provider's discover/load/parse/save logic lives in its own module; this
script only parses CLI arguments, resolves the configured connection, and
dispatches to the matching provider runner.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from bootstrap import (  # noqa: E402
    SUPPORTED_OUTPUT_FORMATS,
    ConnectorConfigurationError,
    connector_definition,
)
from confluence import run_confluence  # noqa: E402
from github import run_github  # noqa: E402
from jira import run_jira  # noqa: E402
from local import run_local  # noqa: E402
from sharepoint import run_sharepoint  # noqa: E402

RUNNERS = {
    "confluence": run_confluence,
    "github": run_github,
    "jira": run_jira,
    "local": run_local,
    "sharepoint": run_sharepoint,
}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one real connector smoke check with configured parsing.",
    )
    parser.add_argument(
        "--connector",
        required=True,
        help="Configured connection ID to exercise. A provider name is also accepted "
        "when exactly one enabled connection uses it.",
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
        help="Directory for saved output (default: tests/connectors/smoke/output).",
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
    try:
        definition = connector_definition(args.connector)
    except ConnectorConfigurationError as exc:
        print(f"[config] {exc}")
        return 2

    run = RUNNERS.get(definition.provider)
    if run is None:
        print(
            f"[config] connection {definition.name!r} uses provider "
            f"{definition.provider!r}, which has no smoke runner"
        )
        return 2
    if definition.provider in {"github", "sharepoint"}:
        if args.output is not None:
            print(f"[{definition.provider}] --output is not supported")
            return 2
        return run(connection_id=definition.name, limit=args.limit)
    return run(
        connection_id=definition.name,
        limit=args.limit,
        output=args.output,
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    raise SystemExit(main())
