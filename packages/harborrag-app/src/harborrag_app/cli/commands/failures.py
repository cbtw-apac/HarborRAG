"""``harborrag failures``: list failed items across runs and retry them."""

from __future__ import annotations

from typing import Annotated

import typer

from harborrag_app.api.v1.ingestion.schemas import FailurePage, FailureQuery, FailureRetryRequest
from harborrag_app.cli.bodies import comma_list, load_body, read_fields
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
from harborrag_app.operations.local import LocalOperations
from harborrag_app.operations.scope import Scope

app = typer.Typer(help="List and retry failed items.", no_args_is_help=True)

ConnectionRefOption = Annotated[
    str | None,
    typer.Option("--connection-ref", help="Only this connection, e.g. engineering/jira-main."),
]
StageOption = Annotated[
    str | None,
    typer.Option("--stage", help="Only this stage: capture, interpret, commit, derive or publish."),
]
ErrorCategoryOption = Annotated[
    str | None, typer.Option("--error-category", help="Only this error category.")
]


@app.command("list")
def list_failures(  # noqa: PLR0913 - the list filters plus the global options
    project_id: Annotated[
        str | None, typer.Option("--project-id", help="Only this project's failures.")
    ] = None,
    connection_ref: ConnectionRefOption = None,
    stage: StageOption = None,
    error_category: ErrorCategoryOption = None,
    show_resolved: Annotated[
        bool, typer.Option("--resolved", help="List resolved failures instead of open ones.")
    ] = False,
    q: Annotated[str | None, typer.Option("--q", help="Search the failures' items.")] = None,
    include_total: Annotated[
        bool, typer.Option("--include-total", help="Also count every failure the filter matches.")
    ] = False,
    limit: LimitOption = DEFAULT_LIMIT,
    cursor: CursorOption = None,
    all_pages: AllOption = False,
    tenant: TenantOption = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """List failed items, newest first; counts cover the whole filter, not just the page."""
    resolved = current_globals(
        tenant=tenant, output=output, profile=profile, config=config, yes=yes
    )
    query = load_body(
        None,
        FailureQuery,
        flags={
            "project_id": project_id,
            "connection_ref": connection_ref,
            "stage": stage,
            "error_category": error_category,
            "resolved": show_resolved,
            "q": q,
            "include_total": include_total,
            "limit": limit,
        },
    )

    async def call(operations: LocalOperations, scope: Scope) -> FailurePage:
        async def fetch(after: str | None) -> FailurePage:
            return await operations.list_failures(scope, query.model_copy(update={"cursor": after}))

        return await collect_pages(fetch, cursor=cursor, follow=all_pages)

    run_operation("list_failures", call, globals=resolved)


@app.command("retry")
def retry_failures(  # noqa: PLR0913 - the selectors plus the global options
    ids: Annotated[
        str | None, typer.Option("--ids", metavar="A,B", help="Retry these failures by id.")
    ] = None,
    connection_ref: ConnectionRefOption = None,
    stage: StageOption = None,
    error_category: ErrorCategoryOption = None,
    tenant: TenantOption = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """Retry failed items by --ids, or every one a filter matches; prints the new runs.

    Give either --ids or the filter options, not both; 409 when nothing qualifies.
    """
    resolved = current_globals(
        tenant=tenant, output=output, profile=profile, config=config, yes=yes
    )
    selector = read_fields(
        None,
        flags={"connection_ref": connection_ref, "stage": stage, "error_category": error_category},
    )
    body = load_body(
        None, FailureRetryRequest, flags={"ids": comma_list(ids), "filter": selector or None}
    )

    async def call(operations: LocalOperations, scope: Scope) -> object:
        return await operations.retry_failures(scope, body)

    run_operation("retry_failures", call, globals=resolved, write=True)
