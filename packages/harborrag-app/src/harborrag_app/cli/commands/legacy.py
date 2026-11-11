"""Hidden stubs for the removed ``source`` and ``ingest`` command groups.

Owns the old-to-new name table; does not run anything. Each stub accepts any arguments,
``--help`` and ``-h`` included, prints one line naming the replacement and exits 2; so does
each group alone. Old names get no alias.

Entry points: SOURCE_APP, INGEST_APP.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Annotated, Any

import typer

from harborrag_app.cli.exit_codes import USAGE

LEGACY: Mapping[tuple[str, str], str] = {
    ("source", "list"): "connections list",
    ("source", "show"): "connections get",
    ("source", "add"): "connections create",
    ("source", "update"): "connections update",
    ("source", "test"): "connections test",
    ("source", "sync"): "ingestions start",
    ("source", "remove"): "connections delete",
    ("ingest", "start"): "ingestions start",
    ("ingest", "run"): "ingestions start --inline",
    ("ingest", "status"): "ingestions get",
    ("ingest", "wait"): "ingestions get --watch",
    ("ingest", "watch"): "ingestions get --watch",
    ("ingest", "pause"): "ingestions pause",
    ("ingest", "resume"): "ingestions resume",
    ("ingest", "cancel"): "ingestions cancel",
}
LEGACY_GROUPS: Mapping[str, str] = {
    "source": "'harborrag connections' (and 'harborrag ingestions start' for syncs)",
    "ingest": "'harborrag ingestions'",
}

_NO_HELP: dict[str, Any] = {"help_option_names": []}
_STUB_SETTINGS: dict[str, Any] = {
    **_NO_HELP,
    "ignore_unknown_options": True,
    "allow_extra_args": True,
}


def _renamed(old: str, replacement: str) -> None:
    """Print that ``harborrag <old>`` was renamed to ``replacement`` and exit 2."""
    typer.echo(f"error: 'harborrag {old}' was renamed; use {replacement}", err=True)
    raise typer.Exit(USAGE)


def _stub(group: str, action: str, replacement: str) -> Callable[[], None]:
    def command() -> None:
        _renamed(f"{group} {action}", f"'harborrag {replacement}'")

    return command


def _group_hint(name: str) -> Callable[..., None]:
    def callback(
        context: typer.Context,
        help_: Annotated[bool, typer.Option("--help", "-h", hidden=True)] = False,
    ) -> None:
        del help_
        if context.invoked_subcommand is None:
            _renamed(name, LEGACY_GROUPS[name])

    return callback


def _group(name: str) -> typer.Typer:
    group = typer.Typer(
        add_completion=False,
        invoke_without_command=True,
        context_settings=_NO_HELP,
        callback=_group_hint(name),
    )
    for (old_group, action), replacement in LEGACY.items():
        if old_group == name:
            group.command(action, hidden=True, context_settings=_STUB_SETTINGS)(
                _stub(name, action, replacement)
            )
    return group


SOURCE_APP = _group("source")
INGEST_APP = _group("ingest")
