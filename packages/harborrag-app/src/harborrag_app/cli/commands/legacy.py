"""Hidden stubs for the removed ``source`` and ``ingest`` command groups.

Owns the old-to-new name table; does not run anything. Each stub accepts any arguments, prints
one line naming the replacement and exits 2. Old names get no alias.

Entry points: SOURCE_APP, INGEST_APP.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

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

_STUB_SETTINGS: dict[str, Any] = {"ignore_unknown_options": True, "allow_extra_args": True}


def _stub(group: str, action: str, replacement: str) -> Callable[[], None]:
    def command() -> None:
        typer.echo(
            f"error: 'harborrag {group} {action}' was renamed; use 'harborrag {replacement}'",
            err=True,
        )
        raise typer.Exit(USAGE)

    return command


def _group(name: str) -> typer.Typer:
    group = typer.Typer(add_completion=False)
    for (old_group, action), replacement in LEGACY.items():
        if old_group == name:
            group.command(action, hidden=True, context_settings=_STUB_SETTINGS)(
                _stub(name, action, replacement)
            )
    return group


SOURCE_APP = _group("source")
INGEST_APP = _group("ingest")
