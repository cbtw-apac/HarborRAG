"""``harborrag ingestions``: start, list, read, follow and control ingestion runs.

``start`` calls the operation behind ``POST /v1/admin/ingestions``. ``--wait`` and ``--watch``
only read the run from this process every 5 seconds. ``--inline`` runs the pipeline here
without Temporal, for development.
"""

from __future__ import annotations

from typing import Annotated, NoReturn

import click
import typer

from harborrag_app.api.v1.admin.connection_schemas import IngestionStartRequest
from harborrag_app.api.v1.ingestion.schemas import IngestionListQuery, IngestionPage
from harborrag_app.cli.bodies import comma_list, load_body
from harborrag_app.cli.commands.connections import RefArgument, parse_ref
from harborrag_app.cli.follow import follow_ingestion, followed
from harborrag_app.cli.globals import (
    ConfigOption,
    Globals,
    OutputOption,
    ProfileOption,
    TenantOption,
    YesOption,
    current_globals,
)
from harborrag_app.cli.inline_run import run_inline
from harborrag_app.cli.invoke import run_operation
from harborrag_app.cli.paging import (
    DEFAULT_LIMIT,
    AllOption,
    CursorOption,
    LimitOption,
    collect_pages,
)
from harborrag_app.operations.local import LocalOperations
from harborrag_app.operations.scope import ConnectionRef, Scope

app = typer.Typer(help="Start, follow and control ingestion runs.", no_args_is_help=True)

IngestionArgument = Annotated[
    str, typer.Argument(metavar="INGESTION_ID", help="Run id, as ingestions list prints it.")
]
ConnectionRefOption = Annotated[
    str | None,
    typer.Option("--connection-ref", help="Only this connection, e.g. engineering/jira-main."),
]
_STATUSES = "queued, running, paused, succeeded, failed or cancelled"


@app.command("start")
def start_ingestion(  # noqa: PLR0913 - the run options plus the global options
    ref: RefArgument,
    mode: Annotated[
        str,
        typer.Option(
            "--mode",
            help="incremental reads what changed since the last sync; force re-reads everything.",
        ),
    ] = "incremental",
    updated_after: Annotated[
        str | None,
        typer.Option(
            "--updated-after",
            metavar="DATE_TIME",
            help="Re-read items changed since then, with a UTC offset: 2026-11-01T00:00:00Z.",
        ),
    ] = None,
    item_refs: Annotated[
        str | None,
        typer.Option(
            "--item-refs", metavar="A,B", help="Only these items, e.g. DEMO-1 (not in v2.0.0)."
        ),
    ] = None,
    wait: Annotated[
        bool,
        typer.Option("--wait", help="Follow the run; exit 8 when it fails or is cancelled."),
    ] = False,
    inline: Annotated[
        bool,
        typer.Option(
            "--inline", help="Run the pipeline in this process without Temporal (development)."
        ),
    ] = False,
    tenant: TenantOption = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """Start an ingestion of a connection and print the run, or follow it with --wait."""
    connection = parse_ref(ref)
    resolved = current_globals(
        tenant=tenant, output=output, profile=profile, config=config, yes=yes
    )
    body = load_body(
        None,
        IngestionStartRequest,
        flags={
            "project_id": connection.project_id,
            "connection_id": connection.connection_id,
            "mode": mode,
            "updated_after": updated_after,
            "item_refs": comma_list(item_refs),
        },
    )
    if inline:
        _refuse_with_inline(wait=wait, item_refs=body.item_refs)
        _start_inline(connection, body, globals=resolved)

    async def call(operations: LocalOperations, scope: Scope) -> object:
        accepted = await operations.start_ingestion(scope, body)
        if not wait:
            return accepted
        run = await follow_ingestion(
            operations, scope, accepted.body.ingestion_id, output=resolved.output
        )
        return followed(run, output=resolved.output)

    run_operation("start_ingestion", call, globals=resolved, write=True)


@app.command("list")
def list_ingestions(  # noqa: PLR0913 - the list filters plus the global options
    project_id: Annotated[
        str | None, typer.Option("--project-id", help="Only this project's runs.")
    ] = None,
    connection_ref: ConnectionRefOption = None,
    status: Annotated[
        str | None, typer.Option("--status", help=f"Only runs that are {_STATUSES}.")
    ] = None,
    created_after: Annotated[
        str | None,
        typer.Option(
            "--created-after", metavar="DATE_TIME", help="Only runs started after this time."
        ),
    ] = None,
    limit: LimitOption = DEFAULT_LIMIT,
    cursor: CursorOption = None,
    all_pages: AllOption = False,
    tenant: TenantOption = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """List ingestion runs, newest first."""
    resolved = current_globals(
        tenant=tenant, output=output, profile=profile, config=config, yes=yes
    )
    query = load_body(
        None,
        IngestionListQuery,
        flags={
            "project_id": project_id,
            "connection_ref": connection_ref,
            "status": status,
            "created_after": created_after,
            "limit": limit,
        },
    )

    async def call(operations: LocalOperations, scope: Scope) -> IngestionPage:
        async def fetch(after: str | None) -> IngestionPage:
            return await operations.list_ingestions(
                scope, query.model_copy(update={"cursor": after})
            )

        return await collect_pages(fetch, cursor=cursor, follow=all_pages)

    run_operation("list_ingestions", call, globals=resolved)


@app.command("get")
def get_ingestion(  # noqa: PLR0913 - the run plus the global options
    ingestion_id: IngestionArgument,
    watch: Annotated[
        bool,
        typer.Option("--watch", help="Refresh until the run ends; exit 8 unless it succeeds."),
    ] = False,
    tenant: TenantOption = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """Show one ingestion run; --watch follows it until it ends."""
    resolved = current_globals(
        tenant=tenant, output=output, profile=profile, config=config, yes=yes
    )

    async def call(operations: LocalOperations, scope: Scope) -> object:
        if not watch:
            return await operations.get_ingestion(scope, ingestion_id)
        run = await follow_ingestion(operations, scope, ingestion_id, output=resolved.output)
        return followed(run, output=resolved.output)

    run_operation("get_ingestion", call, globals=resolved)


@app.command("pause")
def pause_ingestion(  # noqa: PLR0913 - the run plus the global options
    ingestion_id: IngestionArgument,
    tenant: TenantOption = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """Pause a run; 409 invalid_state when its status does not allow it."""
    resolved = current_globals(
        tenant=tenant, output=output, profile=profile, config=config, yes=yes
    )

    async def call(operations: LocalOperations, scope: Scope) -> object:
        return await operations.pause_ingestion(scope, ingestion_id)

    run_operation("pause_ingestion", call, globals=resolved, write=True)


@app.command("resume")
def resume_ingestion(  # noqa: PLR0913 - the run plus the global options
    ingestion_id: IngestionArgument,
    tenant: TenantOption = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """Resume a paused run; 409 invalid_state when its status does not allow it."""
    resolved = current_globals(
        tenant=tenant, output=output, profile=profile, config=config, yes=yes
    )

    async def call(operations: LocalOperations, scope: Scope) -> object:
        return await operations.resume_ingestion(scope, ingestion_id)

    run_operation("resume_ingestion", call, globals=resolved, write=True)


@app.command("cancel")
def cancel_ingestion(  # noqa: PLR0913 - the run plus the global options
    ingestion_id: IngestionArgument,
    tenant: TenantOption = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """Cancel a run; 409 invalid_state when its status does not allow it."""
    resolved = current_globals(
        tenant=tenant, output=output, profile=profile, config=config, yes=yes
    )

    async def call(operations: LocalOperations, scope: Scope) -> object:
        return await operations.cancel_ingestion(scope, ingestion_id)

    run_operation("cancel_ingestion", call, globals=resolved, write=True)


@app.command("retry-failures")
def retry_ingestion_failures(  # noqa: PLR0913 - the run plus the global options
    ingestion_id: IngestionArgument,
    tenant: TenantOption = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """Start a new run of a finished run's failed items and print it."""
    resolved = current_globals(
        tenant=tenant, output=output, profile=profile, config=config, yes=yes
    )

    async def call(operations: LocalOperations, scope: Scope) -> object:
        return await operations.retry_ingestion_failures(scope, ingestion_id, None)

    run_operation("retry_ingestion_failures", call, globals=resolved, write=True)


def _start_inline(
    connection: ConnectionRef, body: IngestionStartRequest, *, globals: Globals
) -> NoReturn:
    """Run the ingestion in this process and print its result; exit 0, or 8 unless it succeeded.

    Raises:
        typer.Exit: Always, as ``run_operation`` does.
    """
    updated_after = body.updated_after.isoformat() if body.updated_after else None

    async def call(operations: LocalOperations, scope: Scope) -> object:
        return await run_inline(
            operations.app_service, scope, connection, mode=body.mode, updated_after=updated_after
        )

    run_operation(None, call, globals=globals, write=True)


def _refuse_with_inline(*, wait: bool, item_refs: list[str]) -> None:
    """Refuse the options an in-process run cannot honour.

    Raises:
        click.UsageError: ``--wait`` or ``--item-refs`` was given with ``--inline``.
    """
    if wait:
        raise click.UsageError("--inline already runs until the run ends; drop --wait")
    if item_refs:
        raise click.UsageError("--inline cannot narrow a run to --item-refs")
