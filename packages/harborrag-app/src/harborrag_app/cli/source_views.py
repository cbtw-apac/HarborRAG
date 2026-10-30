"""Terminal views of the ``harborrag source`` commands."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from harborrag_app.cli.rendering_values import mapping, sequence, text

_STATUS_STYLES = {"active": "green", "needs_reauth": "yellow", "error": "red", "deleting": "dim"}


class SourceViews:
    """Render ``source list`` as a table and point ``source sync`` at its ingestion."""

    def __init__(self, console: Console) -> None:
        self.console = console

    def for_action(self, action: str | None) -> Callable[[Mapping[str, Any]], None] | None:
        """Return the view of ``action``, or None for the generic tree."""
        views: dict[str, Callable[[Mapping[str, Any]], None]] = {
            "list": self.connections,
            "sync": self.ingestion,
        }
        return views.get(action or "")

    def connections(self, data: Mapping[str, Any]) -> None:
        """Print one row per connection, then how to read the next page."""
        table = Table(box=box.SIMPLE, show_header=True, pad_edge=False)
        for column in ("Connection", "Type", "Status", "Last ingestion", "Documents"):
            table.add_column(column, no_wrap=column != "Connection")
        for value in sequence(data.get("items")):
            item = mapping(value)
            status = str(item.get("status", ""))
            last = mapping(item.get("last_ingestion"))
            table.add_row(
                text(item.get("ref"), style="bold"),
                text(item.get("source_type")),
                Text(status, style=_STATUS_STYLES.get(status, "")),
                text(last.get("status") or "never"),
                text(mapping(item.get("stats")).get("documents")),
            )
        self.console.print(table)
        cursor = data.get("next_cursor")
        if cursor:
            self.console.print(Text(f"More: harborrag source list --cursor {cursor}", style="dim"))

    def ingestion(self, data: Mapping[str, Any]) -> None:
        """Print the started ingestion and the command that follows it."""
        run_id = text(data.get("id"))
        self.console.print(
            Text.assemble(
                ("✓ Sync started ", "bold green"),
                (str(data.get("mode", "")), "bold"),
                " for ",
                (str(data.get("connection", "")), "cyan"),
            )
        )
        self.console.print(Text(f"Ingestion {run_id}"))
        self.console.print(Text(f"Follow it: harborrag ingest watch {run_id}", style="dim"))
