"""``harborrag connections``: list, read, write, delete and test connections."""

from __future__ import annotations

import sys
from enum import StrEnum
from typing import Annotated

import typer

from harborrag_app.api.v1.admin.connection_schemas import (
    ConnectionPage,
    ConnectionTestRequest,
    ConnectionWriteRequest,
)
from harborrag_app.cli.bodies import read_document, read_with_secrets, validate_body
from harborrag_app.cli.globals import (
    ConfigOption,
    OutputOption,
    ProfileOption,
    TenantOption,
    YesOption,
    current_globals,
)
from harborrag_app.cli.invoke import run_operation
from harborrag_app.cli.paging import (
    DEFAULT_LIMIT,
    AllOption,
    CursorOption,
    LimitOption,
    collect_pages,
)
from harborrag_app.cli.tenant_guard import confirm_deletion
from harborrag_app.operations.local import LocalOperations
from harborrag_app.operations.scope import ConnectionRef, Scope
from harborrag_app.workflow_control.control_plane.connections import WriteOrigin
from harborrag_core.domain.connection import ConnectionStatus

app = typer.Typer(help="Manage connections.", no_args_is_help=True)

_REF_HELP = "expected PROJECT_ID/CONNECTION_ID, for example default/harborrag-workspace; "
_REF_HINT = "list them with harborrag connections list"
_ORIGIN: WriteOrigin = "operator"


class StatusName(StrEnum):
    """The statuses ``connections list`` filters by."""

    ACTIVE = "active"
    NEEDS_REAUTH = "needs_reauth"
    ERROR = "error"


_STATUSES: dict[StatusName, ConnectionStatus] = {
    StatusName.ACTIVE: "active",
    StatusName.NEEDS_REAUTH: "needs_reauth",
    StatusName.ERROR: "error",
}

RefArgument = Annotated[
    str, typer.Argument(metavar="REF", help="PROJECT_ID/CONNECTION_ID, e.g. engineering/jira-main.")
]
RequiredFile = Annotated[
    str, typer.Option("--file", "-f", metavar="BODY", help="JSON or YAML body; - reads stdin.")
]
SecretPromptOption = Annotated[
    list[str] | None,
    typer.Option(
        "--secret-prompt",
        metavar="FIELD",
        help="Ask for this credential field without echo; repeat for more fields.",
    ),
]


def parse_ref(value: str) -> ConnectionRef:
    """Parse ``PROJECT_ID/CONNECTION_ID``, or fail with the form and where to find refs."""
    try:
        return ConnectionRef.parse(value)
    except ValueError:
        raise typer.BadParameter(_REF_HELP + _REF_HINT, param_hint="REF") from None


@app.command("list")
def list_connections(  # noqa: PLR0913 - the list filters plus the global options
    project_id: Annotated[
        str | None, typer.Option("--project-id", help="Only this project's connections.")
    ] = None,
    source_type: Annotated[
        str | None, typer.Option("--source-type", help="Only connections of this type.")
    ] = None,
    status: Annotated[
        StatusName | None, typer.Option("--status", help="Only connections in this status.")
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
    """List connections; the ones that need attention come first."""
    resolved = current_globals(
        tenant=tenant, output=output, profile=profile, config=config, yes=yes
    )

    async def call(operations: LocalOperations, scope: Scope) -> ConnectionPage:
        async def fetch(after: str | None) -> ConnectionPage:
            return await operations.list_connections(
                scope,
                project_id=project_id,
                source_type=source_type,
                status=None if status is None else _STATUSES[status],
                limit=limit,
                cursor=after,
            )

        return await collect_pages(fetch, cursor=cursor, follow=all_pages)

    run_operation("list_connections", call, globals=resolved)


@app.command("get")
def get_connection(  # noqa: PLR0913 - the ref plus the global options
    ref: RefArgument,
    tenant: TenantOption = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """Show one connection; a stored secret reads back as its status, never its value."""
    connection = parse_ref(ref)
    resolved = current_globals(
        tenant=tenant, output=output, profile=profile, config=config, yes=yes
    )

    async def call(operations: LocalOperations, scope: Scope) -> object:
        return await operations.get_connection(scope, connection)

    run_operation("get_connection", call, globals=resolved)


@app.command("create")
def create_connection(  # noqa: PLR0913 - the body file, prompts and global options
    ref: RefArgument,
    file: RequiredFile,
    secret_prompt: SecretPromptOption = None,
    tenant: TenantOption = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """Create a connection from a body file; fails with 412 when it already exists."""
    connection = parse_ref(ref)
    resolved = current_globals(
        tenant=tenant, output=output, profile=profile, config=config, yes=yes
    )
    body = validate_body(
        ConnectionWriteRequest, read_with_secrets(file, secret_prompt or []), source=file
    )

    async def call(operations: LocalOperations, scope: Scope) -> object:
        return await operations.put_connection(
            scope, connection, body, if_match=None, if_none_match="*", origin=_ORIGIN
        )

    run_operation("put_connection", call, globals=resolved, write=True)


@app.command("update")
def update_connection(  # noqa: PLR0913 - the body file, prompts, ETag and global options
    ref: RefArgument,
    file: RequiredFile,
    secret_prompt: SecretPromptOption = None,
    if_match: Annotated[
        str | None,
        typer.Option("--if-match", help="Update only if the connection still has this ETag."),
    ] = None,
    tenant: TenantOption = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """Replace a connection with a body file; config is replaced whole."""
    connection = parse_ref(ref)
    resolved = current_globals(
        tenant=tenant, output=output, profile=profile, config=config, yes=yes
    )
    body = validate_body(
        ConnectionWriteRequest, read_with_secrets(file, secret_prompt or []), source=file
    )

    async def call(operations: LocalOperations, scope: Scope) -> object:
        etag = if_match or (await operations.get_connection(scope, connection)).etag
        return await operations.put_connection(
            scope, connection, body, if_match=etag, if_none_match=None, origin=_ORIGIN
        )

    run_operation("put_connection", call, globals=resolved, write=True)


@app.command("delete")
def delete_connection(  # noqa: PLR0913 - the purge flag plus the global options
    ref: RefArgument,
    purge: Annotated[
        bool, typer.Option("--purge", help="Also drop the connection's Postgres history.")
    ] = False,
    tenant: TenantOption = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """Delete a connection, its documents and its stored secrets in the background."""
    connection = parse_ref(ref)
    resolved = current_globals(
        tenant=tenant, output=output, profile=profile, config=config, yes=yes
    )
    confirm_deletion(
        f"Delete {connection}, its documents and its stored secrets?",
        yes=resolved.yes,
        interactive=sys.stdin.isatty(),
    )

    async def call(operations: LocalOperations, scope: Scope) -> object:
        return await operations.delete_connection(scope, connection, purge=purge)

    run_operation("delete_connection", call, globals=resolved, write=True)


@app.command("test")
def test_connection(  # noqa: PLR0913 - the body file plus the global options
    ref: RefArgument,
    file: Annotated[
        str | None,
        typer.Option(
            "--file", "-f", metavar="BODY", help="Values to check; default: the saved connection."
        ),
    ] = None,
    tenant: TenantOption = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """Check a connection's values or the saved connection; stores nothing, exits 0 on ok false."""
    connection = parse_ref(ref)
    resolved = current_globals(
        tenant=tenant, output=output, profile=profile, config=config, yes=yes
    )
    body = (
        None
        if file is None
        else validate_body(ConnectionTestRequest, read_document(file), source=file)
    )

    async def call(operations: LocalOperations, scope: Scope) -> object:
        return await operations.test_connection(scope, connection, body, origin=_ORIGIN)

    run_operation("test_connection", call, globals=resolved)
