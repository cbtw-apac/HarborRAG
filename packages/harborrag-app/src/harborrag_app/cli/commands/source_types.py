"""``harborrag source-types``: what each source type needs to connect."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

import typer

from harborrag_app.cli.globals import (
    ConfigOption,
    OutputOption,
    ProfileOption,
    TenantOption,
    YesOption,
    current_globals,
)
from harborrag_app.cli.invoke import run_operation
from harborrag_app.operations.local import LocalOperations
from harborrag_app.operations.scope import Scope

app = typer.Typer(help="List the installed source types.", no_args_is_help=True)


class CategoryName(StrEnum):
    """The categories a source type belongs to."""

    ISSUE_TRACKER = "issue_tracker"
    WIKI = "wiki"
    FILES = "files"
    CODE = "code"


@app.command("list")
def list_source_types(  # noqa: PLR0913 - the category plus the global options
    category: Annotated[
        CategoryName | None, typer.Option("--category", help="Only types of this category.")
    ] = None,
    tenant: TenantOption = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """List each type's auth methods, config_schema and advanced_schema."""
    resolved = current_globals(
        tenant=tenant, output=output, profile=profile, config=config, yes=yes
    )

    async def call(operations: LocalOperations, scope: Scope) -> object:
        return await operations.list_source_types(
            scope, category=None if category is None else category.value
        )

    run_operation("list_source_types", call, globals=resolved)
