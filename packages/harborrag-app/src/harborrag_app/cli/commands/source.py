"""Manage connections from the terminal (`harborrag source ...`).

Each command calls the same connection service as ``/v1/admin/.../connections``,
in this process, so validation, secret handling, audit and status rules match the
API. The control database stays the source of truth; a body file is only input.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

import typer
from rich.prompt import Confirm

from harborrag_app.cli.commands.auth_keys import operator_identity
from harborrag_app.cli.commands.ingest_run import JsonOption
from harborrag_app.cli.environment import refuse_checkout_write
from harborrag_app.cli.runner import invoke
from harborrag_app.cli.source_support import (
    ConnectionCall,
    ConnectionWriteBuilder,
    SourceRef,
    connection_call,
)
from harborrag_app.workflow_control import AppResponse
from harborrag_app.workflow_control.control_plane.connections import (
    ConnectionKey,
    ConnectionService,
    ConnectionWrite,
)

app = typer.Typer(
    no_args_is_help=True,
    help="Manage connections: where and how HarborRAG reads a data source.",
)

Ref = Annotated[
    str,
    typer.Argument(metavar="PROJECT/CONNECTION", help="Such as default/jira-cpm."),
]
Tenant = Annotated[
    str,
    typer.Option(
        "--tenant",
        metavar="TENANT_ID",
        envvar="HARBORRAG_INGESTION_TENANT_ID",
        help="Tenant that owns the connection.",
    ),
]
BodyFile = Annotated[
    Path,
    typer.Option(
        "--file",
        "-f",
        metavar="PATH",
        help="YAML or JSON body: source_type, name, credentials, config, advanced.",
    ),
]


@app.command("list")
def list_sources(  # noqa: PLR0913 - one parameter per documented filter
    context: typer.Context,
    project: Annotated[str | None, typer.Option("--project-id", help="Only this project.")] = None,
    source_type: Annotated[str | None, typer.Option("--type", help="Such as jira.")] = None,
    status: Annotated[
        Literal["active", "needs_reauth", "error", "deleting"] | None,
        typer.Option("--status", help="Only connections in this status."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 20,
    cursor: Annotated[str | None, typer.Option("--cursor", help="From an earlier page.")] = None,
    tenant: Tenant = "DEFAULT",
    as_json: JsonOption = False,
) -> None:
    """List connections, the ones needing attention first."""

    async def operation(connections: ConnectionService) -> dict[str, Any]:
        items, next_cursor = await connections.list_page(
            tenant_id=tenant,
            project_id=project,
            source_type=source_type,
            status=status,
            limit=limit,
            cursor=cursor,
        )
        return {"items": items, "next_cursor": next_cursor}

    _invoke(context, "list", operation, as_json=as_json)


@app.command("show")
def show(
    context: typer.Context, ref: Ref, tenant: Tenant = "DEFAULT", as_json: JsonOption = False
) -> None:
    """Show one connection; secrets show their status, never the value."""

    key = _key(ref, tenant)

    async def operation(connections: ConnectionService) -> dict[str, Any]:
        view, version = await connections.get(key)
        return {**view, "version": version}

    _invoke(context, "show", operation, as_json=as_json)


@app.command("add")
def add(  # noqa: PLR0913 - one parameter per documented option
    context: typer.Context,
    ref: Ref,
    body: BodyFile,
    sync_now: Annotated[
        bool, typer.Option("--sync-now", help="Start the first ingestion right away.")
    ] = False,
    tenant: Tenant = "DEFAULT",
    as_json: JsonOption = False,
) -> None:
    """Create a connection from a body file; refuses an id that already exists."""

    refuse_checkout_write()
    key, write = _key(ref, tenant), _write(body)

    async def operation(connections: ConnectionService) -> dict[str, Any]:
        result = await connections.put(
            key, write, actor=operator_identity(), create_only=True, sync_now=sync_now
        )
        return {**result.connection, "version": result.version, "ingestion": result.ingestion}

    _invoke(context, "add", operation, as_json=as_json)


@app.command("update")
def update(  # noqa: PLR0913 - one parameter per documented option
    context: typer.Context,
    ref: Ref,
    body: BodyFile,
    if_version: Annotated[
        int | None,
        typer.Option(
            "--if-version",
            min=1,
            help="Fail if the stored version differs (default: the version read first).",
        ),
    ] = None,
    tenant: Tenant = "DEFAULT",
    as_json: JsonOption = False,
) -> None:
    """Overwrite a connection; config and advanced are replaced, omitted secrets are kept."""

    refuse_checkout_write()
    key, write = _key(ref, tenant), _write(body)

    async def operation(connections: ConnectionService) -> dict[str, Any]:
        _view, version = await connections.get(key)
        result = await connections.put(
            key, write, actor=operator_identity(), expected_version=if_version or version
        )
        return {**result.connection, "version": result.version}

    _invoke(context, "update", operation, as_json=as_json)


@app.command("test")
def test(
    context: typer.Context,
    ref: Ref,
    body: Annotated[
        Path | None,
        typer.Option(
            "--file", "-f", metavar="PATH", help="Test these values instead; saves nothing."
        ),
    ] = None,
    tenant: Tenant = "DEFAULT",
    as_json: JsonOption = False,
) -> None:
    """Check sign-in, reachability and a sample of items; exits 1 when a check fails."""

    key = _key(ref, tenant)
    write = _write(body) if body is not None else None

    async def operation(connections: ConnectionService) -> dict[str, Any]:
        return await connections.test(key, write)

    _invoke(context, "test", operation, as_json=as_json, succeeded=_passed)


@app.command("sync")
def sync(
    context: typer.Context,
    ref: Ref,
    mode: Annotated[
        Literal["incremental", "full"],
        typer.Option("--mode", help="incremental reads changes; full re-reads everything."),
    ] = "incremental",
    tenant: Tenant = "DEFAULT",
    as_json: JsonOption = False,
) -> None:
    """Start an ingestion of the connection; refuses while one is running."""

    refuse_checkout_write()
    key = _key(ref, tenant)

    async def operation(connections: ConnectionService) -> dict[str, Any]:
        return await connections.sync(key, actor=operator_identity(), mode=mode)

    _invoke(context, "sync", operation, as_json=as_json)


@app.command("remove")
def remove(  # noqa: PLR0913 - one parameter per documented option
    context: typer.Context,
    ref: Ref,
    purge: Annotated[
        bool, typer.Option("--purge", help="Also drop its ingestion history.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask to confirm.")] = False,
    tenant: Tenant = "DEFAULT",
    as_json: JsonOption = False,
) -> None:
    """Delete a connection, its documents and its stored secrets in the background."""

    refuse_checkout_write()
    key = _key(ref, tenant)
    _confirm_removal(ref, yes=yes)

    async def operation(connections: ConnectionService) -> dict[str, Any]:
        return {"job_id": await connections.delete(key, actor=operator_identity(), purge=purge)}

    _invoke(context, "remove", operation, as_json=as_json)


def _invoke(
    context: typer.Context,
    action: str,
    operation: ConnectionCall,
    *,
    as_json: bool,
    succeeded: Callable[[AppResponse], bool] | None = None,
) -> None:
    """Run ``operation`` on the connection service through the shared CLI runner."""
    invoke(
        connection_call(operation),
        context=context,
        command="source",
        action=action,
        as_json=as_json,
        succeeded=succeeded,
    )


def _key(ref: str, tenant: str) -> ConnectionKey:
    """Parse ``ref`` or stop with a usage error."""
    try:
        return SourceRef.parse(ref).key(tenant)
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="PROJECT/CONNECTION") from None


def _write(path: Path) -> ConnectionWrite:
    """Read a body file or stop with a usage error."""
    try:
        return ConnectionWriteBuilder().from_file(path).build()
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="--file") from None


def _confirm_removal(ref: str, *, yes: bool) -> None:
    """Ask on a terminal; without one, require ``--yes``."""
    if yes:
        return
    if not sys.stdin.isatty():
        typer.echo("harborrag source remove: pass --yes to delete without a terminal", err=True)
        raise typer.Exit(2)
    if not Confirm.ask(f"Delete {ref}, its documents and its stored secrets?", default=False):
        typer.echo("Left the connection in place.", err=True)
        raise typer.Exit(1)


def _passed(response: AppResponse) -> bool:
    """True when a connection test answered and every check passed."""
    return response.ok and bool(response.data.get("ok"))
