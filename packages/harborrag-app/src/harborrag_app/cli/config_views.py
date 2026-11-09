"""Terminal views of the ``harborrag config`` commands."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from rich.console import Console

from harborrag_app.cli.rendering_values import mapping, sequence


class ConfigViews:
    """Render ``config import`` as one line per change and ``config export`` as YAML."""

    def __init__(self, console: Console) -> None:
        self.console = console

    def for_action(self, action: str | None) -> Callable[[Mapping[str, Any]], None] | None:
        """Return the view of ``action``, or None for the generic tree."""
        views: dict[str, Callable[[Mapping[str, Any]], None]] = {
            "import": self.plan,
            "export": self.exported,
        }
        return views.get(action or "")

    def plan(self, data: Mapping[str, Any]) -> None:
        """Print the skipped keys, the changes, and whether they were written."""
        for value in sequence(data.get("skipped")):
            item = mapping(value)
            reason = str(item.get("reason", "")).replace("_", "-")
            self._line(f"skipped {item.get('level')} {item.get('key')}: {reason}")
        changes = sequence(data.get("changes"))
        for value in changes:
            item = mapping(value)
            self._line(
                f"{item.get('level')}  {item.get('key')}  "
                f"{json.dumps(item.get('from'))} → {json.dumps(item.get('to'))}  "
                f"({item.get('applies')})"
            )
        if not changes:
            self._line("no changes")
        elif data.get("applied"):
            self._line(f"applied as {data.get('revision')}")
        else:
            self._line("dry run; add --apply to write these changes")

    def exported(self, data: Mapping[str, Any]) -> None:
        """Print the YAML, or the file it was written to."""
        if data.get("output"):
            self._line(f"wrote {data['output']}")
            return
        self.console.print(str(data.get("yaml", "")), end="", markup=False, soft_wrap=True)

    def _line(self, message: str) -> None:
        self.console.print(message, markup=False, soft_wrap=True)
