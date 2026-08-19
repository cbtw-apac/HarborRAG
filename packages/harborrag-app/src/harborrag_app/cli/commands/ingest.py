"""Temporal-backed ingestion CLI commands."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from harborrag_app.cli.runner import invoke, invoke_dashboard
from harborrag_app.workflow_control import AppResponse, BaseAppService

app = typer.Typer(
    help="Start, inspect, and control durable Temporal ingestion workflows.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 120},
)

JsonOption = Annotated[
    bool,
    typer.Option(
        "--json",
        help="Emit the stable machine-readable response envelope.",
    ),
]


@app.command(help="Start a new ingestion run.", rich_help_panel="Submit")
def start(  # noqa: PLR0913 - Typer requires one parameter per public option
    context: typer.Context,
    connector_name: Annotated[
        str,
        typer.Option(
            # --connector-id is the truer name: the value is a key under `connectors:`
            # in config/connectors.yaml, which that file documents as the public
            # connection_id. --connector stays as the original spelling.
            "--connector-id",
            "--connector",
            metavar="NAME",
            help=(
                "Configured connector name from config/connectors.yaml "
                "(for example jira-main), not a provider type."
            ),
        ),
    ],
    tenant_id: Annotated[
        str,
        typer.Option(
            "--tenant",
            metavar="TENANT_ID",
            help="Tenant that owns the ingestion run.",
        ),
    ] = "DEFAULT",
    run_id: Annotated[
        str | None,
        typer.Option("--run-id", help="Stable run ID; generated when omitted."),
    ] = None,
    connection_id: Annotated[
        str | None,
        typer.Option(
            "--connection-id",
            help="Stable logical connector connection ID; defaults to the connector name.",
        ),
    ] = None,
    source_scope_id: Annotated[
        str | None,
        typer.Option(
            "--source-scope-id",
            help="Stable source scope ID; derived from the query when omitted.",
        ),
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
        typer.Option(
            "--recursive/--no-recursive",
            help="Traverse the configured source recursively.",
        ),
    ] = True,
    updated_after: Annotated[
        str | None,
        typer.Option(
            "--updated-after",
            help="Only discover content updated after this ISO-8601 timestamp.",
        ),
    ] = None,
    max_artifacts: Annotated[
        int | None,
        typer.Option(
            "--limit",
            min=1,
            metavar="COUNT",
            help="Stop after ingesting at most this many discovered artifacts.",
        ),
    ] = None,
    include_attachments: Annotated[
        bool,
        typer.Option(
            "--attachments/--no-attachments",
            help="Admit source attachments as independent documents.",
        ),
    ] = True,
    filters_json: Annotated[
        str,
        typer.Option(
            "--filters-json",
            metavar="JSON",
            help="Connector-specific discovery filters as a JSON object.",
        ),
    ] = "{}",
    force_reprocess: Annotated[
        bool,
        typer.Option(
            "--force-reprocess",
            help="Reprocess admitted documents even when source descriptors are unchanged.",
        ),
    ] = False,
    batch_size: Annotated[
        int | None,
        typer.Option(
            "--batch-size",
            min=1,
            max=300,
            metavar="COUNT",
            help=(
                "Documents per SourceBatchWorkflow child; "
                "defaults to config/temporal.yaml's ingestion.batch_size."
            ),
        ),
    ] = None,
    document_concurrency: Annotated[
        int | None,
        typer.Option(
            "--document-concurrency",
            min=1,
            max=100,
            metavar="COUNT",
            help=(
                "Concurrent DocumentIngestionWorkflow children per wave; "
                "defaults to config/temporal.yaml's ingestion.document_concurrency."
            ),
        ),
    ] = None,
    wait: Annotated[
        bool,
        typer.Option("--wait", help="Wait for completion and display the final summary."),
    ] = False,
    as_json: JsonOption = False,
) -> None:
    """Submit one canonical ingestion workflow to Temporal."""

    invoke(
        lambda service: service.start_ingestion(
            tenant_id=tenant_id,
            connector_name=connector_name,
            run_id=run_id,
            connection_id=connection_id,
            source_scope_id=source_scope_id,
            path=path,
            pattern=pattern,
            recursive=recursive,
            updated_after=updated_after,
            max_artifacts=max_artifacts,
            include_attachments=include_attachments,
            filters=_filters(filters_json),
            force_reprocess=force_reprocess,
            batch_size=batch_size,
            document_concurrency=document_concurrency,
            wait=wait,
        ),
        context=context,
        command="ingest",
        action="start",
        as_json=as_json,
    )


@app.command(help="Query a running ingestion.", rich_help_panel="Observe")
def status(
    context: typer.Context,
    run_id: Annotated[str, typer.Argument(metavar="RUN_ID", help="Ingestion run ID.")],
    as_json: JsonOption = False,
) -> None:
    """Display the current run, stage, progress, and artifact state."""

    invoke(
        lambda service: service.ingestion_status(run_id),
        context=context,
        command="ingest",
        action="status",
        as_json=as_json,
    )


@app.command(help="Wait for the final run result.", rich_help_panel="Observe")
def wait(
    context: typer.Context,
    run_id: Annotated[str, typer.Argument(metavar="RUN_ID", help="Ingestion run ID.")],
    as_json: JsonOption = False,
) -> None:
    """Block until Temporal returns the terminal ingestion summary."""

    invoke(
        lambda service: service.ingestion_result(run_id),
        context=context,
        command="ingest",
        action="wait",
        as_json=as_json,
    )


@app.command(help="Open the live ingestion dashboard.", rich_help_panel="Observe")
def watch(
    context: typer.Context,
    run_id: Annotated[str, typer.Argument(metavar="RUN_ID", help="Ingestion run ID.")],
    refresh_seconds: Annotated[
        float,
        typer.Option(
            "--refresh",
            min=0.25,
            max=60.0,
            metavar="SECONDS",
            help="Status polling interval.",
        ),
    ] = 1.0,
) -> None:
    """Launch a full-screen dashboard with live progress and run controls."""

    invoke_dashboard(
        run_id,
        context=context,
        refresh_seconds=refresh_seconds,
    )


@app.command(help="Pause an ingestion run.", rich_help_panel="Control")
def pause(
    context: typer.Context,
    run_id: Annotated[str, typer.Argument(metavar="RUN_ID", help="Ingestion run ID.")],
    as_json: JsonOption = False,
) -> None:
    """Request a durable pause between safe workflow boundaries."""

    _control(context, run_id, "pause", as_json=as_json)


@app.command(help="Resume an ingestion run.", rich_help_panel="Control")
def resume(
    context: typer.Context,
    run_id: Annotated[str, typer.Argument(metavar="RUN_ID", help="Ingestion run ID.")],
    as_json: JsonOption = False,
) -> None:
    """Resume a paused ingestion workflow."""

    _control(context, run_id, "resume", as_json=as_json)


@app.command(help="Cancel an ingestion run.", rich_help_panel="Control")
def cancel(
    context: typer.Context,
    run_id: Annotated[str, typer.Argument(metavar="RUN_ID", help="Ingestion run ID.")],
    as_json: JsonOption = False,
) -> None:
    """Cancel at a safe workflow boundary, preserving durable task state."""

    _control(context, run_id, "cancel", as_json=as_json)


def _control(
    context: typer.Context,
    run_id: str,
    action: str,
    *,
    as_json: bool,
) -> None:
    invoke(
        lambda service: _control_request(
            service,
            run_id,
            action,
        ),
        context=context,
        command="ingest",
        action=action,
        as_json=as_json,
    )


async def _control_request(
    service: BaseAppService,
    run_id: str,
    action: str,
) -> AppResponse:
    return await service.control_ingestion(run_id, action)


def _filters(value: str) -> dict[str, object]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("--filters-json must encode a JSON object")
    return {str(key): item for key, item in parsed.items()}
