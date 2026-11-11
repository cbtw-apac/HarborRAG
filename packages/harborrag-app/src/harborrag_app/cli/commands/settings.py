"""``harborrag settings``: describe, read and change runtime settings."""

from __future__ import annotations

import json
from typing import Annotated

import click
import typer

from harborrag_app.cli.bodies import read_document
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

app = typer.Typer(help="Read and change settings.", no_args_is_help=True)

_LEVEL_HELP = "tenant, project:ID or connection:PROJECT_ID/CONNECTION_ID."
LevelOption = Annotated[str, typer.Option("--level", metavar="LEVEL", help=_LEVEL_HELP)]


def parse_setting(argument: str) -> tuple[str, object]:
    """Split ``KEY=VALUE`` at the first ``=``; the value is JSON when it parses, else text.

    Args:
        argument: For example ``chunking.max_tokens=384``, ``parsing.pdf_parser=liteparse`` or
            ``retrieval.top_k=null``, which removes the override.

    Returns:
        The key and its value; ``null`` is ``None``.

    Raises:
        click.BadParameter: ``argument`` has no ``=``.
    """
    key, separator, text = argument.partition("=")
    if not separator or not key:
        raise click.BadParameter("expected KEY=VALUE, for example chunking.max_tokens=384")
    try:
        return key, json.loads(text)
    except ValueError:
        return key, text


@app.command("schema")
def settings_schema(  # noqa: PLR0913 - only the global options
    tenant: TenantOption = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """Describe every setting: type, range, levels and when a change applies."""
    resolved = current_globals(
        tenant=tenant, output=output, profile=profile, config=config, yes=yes
    )

    async def call(operations: LocalOperations, scope: Scope) -> object:
        return await operations.settings_schema(scope)

    run_operation("settings_schema", call, globals=resolved)


@app.command("get")
def get_settings(  # noqa: PLR0913 - the level flags plus the global options
    level: LevelOption = "tenant",
    effective: Annotated[
        bool, typer.Option("--effective", help="Show every key's effective value and origin.")
    ] = False,
    tenant: TenantOption = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """Show the overrides stored at a level, or every key's effective value."""
    resolved = current_globals(
        tenant=tenant, output=output, profile=profile, config=config, yes=yes
    )

    async def call(operations: LocalOperations, scope: Scope) -> object:
        return await operations.read_settings(scope, level=level, effective=effective)

    run_operation("read_settings", call, globals=resolved)


@app.command("set")
def set_settings(  # noqa: PLR0913 - the change sources, flags and global options
    changes: Annotated[
        list[str] | None,
        typer.Argument(metavar="[KEY=VALUE]...", help="Changes; KEY=null removes an override."),
    ] = None,
    file: Annotated[
        str | None,
        typer.Option("--file", "-f", metavar="CHANGES", help="JSON or YAML object of changes."),
    ] = None,
    level: LevelOption = "tenant",
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show the changes without saving them.")
    ] = False,
    if_match: Annotated[
        str | None,
        typer.Option("--if-match", help="Change only if the settings still have this ETag."),
    ] = None,
    tenant: TenantOption = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """Save changes at a level as one revision, from KEY=VALUE pairs or a file."""
    body = _changes(changes or [], file)
    resolved = current_globals(
        tenant=tenant, output=output, profile=profile, config=config, yes=yes
    )

    async def call(operations: LocalOperations, scope: Scope) -> object:
        etag = (
            if_match or (await operations.read_settings(scope, level=level, effective=False)).etag
        )
        return await operations.patch_settings(
            scope, body, level=level, dry_run=dry_run, reindex=False, if_match=etag
        )

    run_operation("patch_settings", call, globals=resolved, write=not dry_run)


def _changes(pairs: list[str], file: str | None) -> dict[str, object]:
    """The changes of the pairs or of the file, not both.

    Raises:
        click.UsageError: Both or neither are given, or the file cannot be read.
    """
    if pairs and file is not None:
        raise click.UsageError("give either KEY=VALUE changes or --file, not both")
    if not pairs and file is None:
        raise click.UsageError("nothing to change: give KEY=VALUE changes or --file")
    if file is not None:
        return read_document(file)
    return dict(parse_setting(pair) for pair in pairs)
