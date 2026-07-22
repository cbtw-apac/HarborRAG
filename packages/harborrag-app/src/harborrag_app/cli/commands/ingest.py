"""Temporal-backed ingestion CLI commands."""

from __future__ import annotations

from typing import Annotated

import typer

from harborrag_app.cli.runner import invoke, invoke_dashboard
from harborrag_app.services.base import AppResponse, BaseAppService

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
def start(
    context: typer.Context,
    tenant_id: Annotated[
        str,
        typer.Option(
            "--tenant",
            metavar="TENANT_ID",
            help="Tenant that owns the ingestion run.",
        ),
    ],
    connector_name: Annotated[
        str,
        typer.Option(
            "--connector",
            metavar="NAME",
            help="Enabled connector name from the runtime configuration.",
        ),
    ],
    run_id: Annotated[
        str | None,
        typer.Option("--run-id", help="Stable run ID; generated when omitted."),
    ] = None,
    manifest_id: Annotated[
        str | None,
        typer.Option("--manifest-id", help="Manifest ID; generated when omitted."),
    ] = None,
    generation_id: Annotated[
        str | None,
        typer.Option("--generation-id", help="Index generation ID; generated when omitted."),
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
            manifest_id=manifest_id,
            generation_id=generation_id,
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
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Cancel immediately instead of requesting graceful cancellation.",
        ),
    ] = False,
    as_json: JsonOption = False,
) -> None:
    """Cancel gracefully by default, preserving reconciliation behavior."""

    _control(context, run_id, "cancel", graceful=not force, as_json=as_json)


@app.command(help="Retry selected failed artifacts.", rich_help_panel="Control")
def retry(
    context: typer.Context,
    run_id: Annotated[str, typer.Argument(metavar="RUN_ID", help="Ingestion run ID.")],
    artifact_ids: Annotated[
        list[str],
        typer.Option(
            "--artifact",
            metavar="ARTIFACT_ID",
            help="Artifact to retry; repeat for multiple artifacts.",
        ),
    ],
    as_json: JsonOption = False,
) -> None:
    """Resubmit one or more failed artifacts through their durable workflow."""

    _control(
        context,
        run_id,
        "retry",
        artifact_ids=tuple(artifact_ids),
        as_json=as_json,
    )


def _control(
    context: typer.Context,
    run_id: str,
    action: str,
    *,
    artifact_ids: tuple[str, ...] = (),
    graceful: bool = True,
    as_json: bool,
) -> None:
    invoke(
        lambda service: _control_request(
            service,
            run_id,
            action,
            artifact_ids=artifact_ids,
            graceful=graceful,
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
    *,
    artifact_ids: tuple[str, ...],
    graceful: bool,
) -> AppResponse:
    return await service.control_ingestion(
        run_id,
        action,
        artifact_ids=artifact_ids,
        graceful=graceful,
    )
