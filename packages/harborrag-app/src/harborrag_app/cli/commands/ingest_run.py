"""Direct-mode ingestion command (`harborrag ingest run`), split out of ingest.py."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from harborrag_app.cli.following import invoke_run
from harborrag_app.workflow_control.ingestion.direct import new_run_id

JsonOption = Annotated[
    bool,
    typer.Option(
        "--json",
        help="Emit the stable machine-readable response envelope.",
    ),
]


def register_run(app: typer.Typer) -> None:
    """Attach the ``run`` command to the ``ingest`` group."""

    app.command(
        "run",
        help="Run an ingestion inline, without Temporal.",
        rich_help_panel="Submit",
    )(run)


def run(  # noqa: PLR0913 - Typer requires one parameter per public option
    context: typer.Context,
    connector_name: Annotated[
        str,
        typer.Argument(
            metavar="CONNECTOR",
            help="Connector name from config/connectors.yaml (for example workspace).",
        ),
    ],
    tenant_id: Annotated[
        str,
        typer.Option("--tenant", metavar="TENANT_ID", help="Tenant that owns the run."),
    ] = "DEFAULT",
    run_id: Annotated[
        str | None,
        typer.Option("--run-id", help="Stable run ID; generated when omitted."),
    ] = None,
    connection_id: Annotated[
        str | None,
        typer.Option(
            "--connection-id", help="Logical connection ID; defaults to the connector name."
        ),
    ] = None,
    source_scope_id: Annotated[
        str | None,
        typer.Option("--source-scope-id", help="Stable source scope ID; derived when omitted."),
    ] = None,
    path: Annotated[
        str | None,
        typer.Option("--path", help="Connector-specific discovery path."),
    ] = None,
    pattern: Annotated[
        str | None,
        typer.Option("--pattern", help="Connector-specific discovery pattern."),
    ] = None,
    recursive: Annotated[
        bool,
        typer.Option("--recursive/--no-recursive", help="Traverse the source recursively."),
    ] = True,
    updated_after: Annotated[
        str | None,
        typer.Option("--updated-after", help="Only content updated after this ISO-8601 timestamp."),
    ] = None,
    max_artifacts: Annotated[
        int | None,
        typer.Option("--limit", min=1, metavar="COUNT", help="Stop after this many artifacts."),
    ] = None,
    include_attachments: Annotated[
        bool,
        typer.Option("--attachments/--no-attachments", help="Admit attachments as documents."),
    ] = True,
    filters_json: Annotated[
        str,
        typer.Option("--filters-json", metavar="JSON", help="Connector filters as a JSON object."),
    ] = "{}",
    force_reprocess: Annotated[
        bool,
        typer.Option("--force-reprocess", help="Reprocess unchanged documents."),
    ] = False,
    events: Annotated[
        bool,
        typer.Option("--events", help="Stream NDJSON progress events, then the final envelope."),
    ] = False,
    as_json: JsonOption = False,
) -> None:
    """Execute the ingestion in this process with live inline progress."""

    resolved_run_id = run_id or new_run_id()
    invoke_run(
        lambda service: service.run_ingestion(
            tenant_id=tenant_id,
            connector_name=connector_name,
            run_id=resolved_run_id,
            connection_id=connection_id,
            source_scope_id=source_scope_id,
            path=path,
            pattern=pattern,
            recursive=recursive,
            updated_after=updated_after,
            max_artifacts=max_artifacts,
            include_attachments=include_attachments,
            filters=parse_filters(filters_json),
            force_reprocess=force_reprocess,
        ),
        context=context,
        run_id=resolved_run_id,
        label=connector_name,
        as_json=as_json,
        events=events,
    )


def parse_filters(value: str) -> dict[str, object]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("--filters-json must encode a JSON object")
    return {str(key): item for key, item in parsed.items()}


__all__ = ["JsonOption", "parse_filters", "register_run", "run"]
