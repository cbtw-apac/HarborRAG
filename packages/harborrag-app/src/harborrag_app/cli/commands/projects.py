"""``harborrag projects``: list, read, create, update and delete projects."""

from __future__ import annotations

import sys
from typing import Annotated

import typer

from harborrag_app.api.v1.admin.connection_schemas import ProjectPage, ProjectWriteRequest
from harborrag_app.cli.bodies import load_body
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
from harborrag_app.operations.scope import Scope

app = typer.Typer(help="Manage projects.", no_args_is_help=True)

ProjectArgument = Annotated[
    str, typer.Argument(metavar="PROJECT", help="Project id, for example harborrag-workspace.")
]
NameOption = Annotated[
    str | None, typer.Option("--project-name", help="Display name (required unless -f sets it).")
]
DescriptionOption = Annotated[
    str | None, typer.Option("--project-description", help="What the project holds.")
]
FileOption = Annotated[
    str | None,
    typer.Option("--file", "-f", metavar="BODY", help="JSON or YAML body; - reads stdin."),
]
IfMatchOption = Annotated[
    str | None, typer.Option("--if-match", help="Update only if the project still has this ETag.")
]


@app.command("list")
def list_projects(  # noqa: PLR0913 - the list filters plus the global options
    q: Annotated[str | None, typer.Option("--q", help="Search project names.")] = None,
    limit: LimitOption = DEFAULT_LIMIT,
    cursor: CursorOption = None,
    all_pages: AllOption = False,
    tenant: TenantOption = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """List the projects in the tenant."""
    resolved = current_globals(
        tenant=tenant, output=output, profile=profile, config=config, yes=yes
    )

    async def call(operations: LocalOperations, scope: Scope) -> ProjectPage:
        async def fetch(after: str | None) -> ProjectPage:
            return await operations.list_projects(scope, q=q, limit=limit, cursor=after)

        return await collect_pages(fetch, cursor=cursor, follow=all_pages)

    run_operation("list_projects", call, globals=resolved)


@app.command("get")
def get_project(  # noqa: PLR0913 - the project plus the global options
    project: ProjectArgument,
    tenant: TenantOption = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """Show one project."""
    resolved = current_globals(
        tenant=tenant, output=output, profile=profile, config=config, yes=yes
    )

    async def call(operations: LocalOperations, scope: Scope) -> object:
        return await operations.get_project(scope, project)

    run_operation("get_project", call, globals=resolved)


@app.command("create")
def create_project(  # noqa: PLR0913 - the body's flags plus the global options
    project: ProjectArgument,
    project_name: NameOption = None,
    project_description: DescriptionOption = None,
    file: FileOption = None,
    tenant: TenantOption = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """Create a project; fails with 412 when it already exists."""
    resolved = current_globals(
        tenant=tenant, output=output, profile=profile, config=config, yes=yes
    )
    body = load_body(
        file,
        ProjectWriteRequest,
        flags={"project_name": project_name, "project_description": project_description},
    )

    async def call(operations: LocalOperations, scope: Scope) -> object:
        return await operations.put_project(scope, project, body, if_match=None, if_none_match="*")

    run_operation("put_project", call, globals=resolved, write=True)


@app.command("update")
def update_project(  # noqa: PLR0913 - the body's flags plus the global options
    project: ProjectArgument,
    project_name: NameOption = None,
    project_description: DescriptionOption = None,
    file: FileOption = None,
    if_match: IfMatchOption = None,
    tenant: TenantOption = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """Replace a project's body with the flags and file, as the HTTP PUT does.

    A field left out is cleared, so --project-name is required unless -f sets it.
    """
    resolved = current_globals(
        tenant=tenant, output=output, profile=profile, config=config, yes=yes
    )
    body = load_body(
        file,
        ProjectWriteRequest,
        flags={"project_name": project_name, "project_description": project_description},
    )

    async def call(operations: LocalOperations, scope: Scope) -> object:
        etag = if_match or (await operations.get_project(scope, project)).etag
        return await operations.put_project(scope, project, body, if_match=etag, if_none_match=None)

    run_operation("put_project", call, globals=resolved, write=True)


@app.command("delete")
def delete_project(  # noqa: PLR0913 - the cascade flag plus the global options
    project: ProjectArgument,
    cascade: Annotated[
        bool, typer.Option("--cascade", help="Also remove its connections, documents and secrets.")
    ] = False,
    tenant: TenantOption = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """Delete a project; with --cascade the removal runs in the background and prints its job."""
    resolved = current_globals(
        tenant=tenant, output=output, profile=profile, config=config, yes=yes
    )
    scope_of_removal = " and everything in it" if cascade else ""
    confirm_deletion(
        f"Delete project {project}{scope_of_removal}?",
        yes=resolved.yes,
        interactive=sys.stdin.isatty(),
    )

    async def call(operations: LocalOperations, scope: Scope) -> object:
        return await operations.delete_project(scope, project, cascade=cascade)

    run_operation("delete_project", call, globals=resolved, write=True)
