"""Short alias for querying ingestion run status."""

from __future__ import annotations

from typing import Annotated

import typer

from harborrag_app.cli.runner import invoke


def command(
    context: typer.Context,
    run_id: Annotated[str, typer.Argument(metavar="RUN_ID", help="Ingestion run ID.")],
    as_json: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit the stable machine-readable response envelope.",
        ),
    ] = False,
) -> None:
    """Query a run without spelling the longer `ingest status` command."""

    invoke(
        lambda service: service.ingestion_status(run_id),
        context=context,
        command="status",
        action="status",
        as_json=as_json,
    )
