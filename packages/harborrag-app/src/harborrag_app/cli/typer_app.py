"""The one place CLI Typer apps are built, so they all share the same defaults.

Click 8.4 adds a "Did you mean" hint to an unknown command itself, so Typer's own hint is
switched off to keep it printed once.

Start from: new_typer.
"""

from __future__ import annotations

from typing import Any

import typer


def new_typer(**options: Any) -> typer.Typer:  # noqa: ANN401 - forwards Typer's own keyword options
    """Build a Typer app that leaves the unknown-command hint to Click.

    Args:
        **options: Keyword arguments passed to ``typer.Typer``.

    Returns:
        The new app.
    """
    return typer.Typer(suggest_commands=False, **options)
