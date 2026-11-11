"""Rich presentation for operator-facing CLI responses."""

from __future__ import annotations

from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from harborrag_app.cli.rendering_values import mapping, sequence, text
from harborrag_app.workflow_control import AppResponse

_CHECK_GLYPHS = {
    "ok": ("✓", "green"),
    "fail": ("✗", "red"),
    "warn": ("!", "yellow"),
    "skip": ("–", "dim"),
}


class CliRenderer:
    """Render stable application responses for a human-operated terminal."""

    def __init__(
        self,
        *,
        no_color: bool = False,
        console: Console | None = None,
        error_console: Console | None = None,
    ) -> None:
        self.console = console or Console(
            no_color=no_color,
            highlight=False,
            soft_wrap=False,
        )
        self.error_console = error_console or Console(
            stderr=True,
            no_color=no_color,
            highlight=False,
            soft_wrap=False,
        )

    @contextmanager
    def operation(self, message: str, *, enabled: bool = True) -> Generator[None, None, None]:
        """Display a transient spinner while a runtime operation is pending."""

        if not enabled:
            yield
            return
        with self.console.status(message, spinner="dots"):
            yield

    def render(
        self,
        response: AppResponse,
        *,
        command: str,
        action: str | None = None,
    ) -> None:
        """Select a concise visualization for one CLI response."""

        if not response.ok:
            self._error(response)
            return
        view = self._view_for(command, action)
        if view is None:
            self.console.print(self._tree("Result", response.data))
            return
        view(response.data)

    def _view_for(
        self,
        command: str,
        action: str | None,
    ) -> Callable[[Mapping[str, Any]], None] | None:
        """Select the view for a command, or None for the generic tree."""

        del action
        return self._doctor if command == "doctor" else None

    def _doctor(self, data: Mapping[str, Any]) -> None:
        table = Table(box=box.SIMPLE, expand=True, show_header=True, pad_edge=False)
        table.add_column("", width=2, justify="center")
        table.add_column("Check", style="bold", no_wrap=True)
        table.add_column("Detail", ratio=1)
        hints: list[str] = []
        for value in sequence(data.get("checks")):
            check = mapping(value)
            status = str(check.get("status", "skip"))
            glyph, style = _CHECK_GLYPHS.get(status, ("?", "dim"))
            table.add_row(
                Text(glyph, style=style),
                text(check.get("name")),
                text(check.get("detail"), style=None if status == "ok" else style),
            )
            hint = str(check.get("hint") or "")
            if status == "fail" and hint and hint not in hints:
                hints.append(hint)
        # `ready` is the exit-code decision; the fail count would contradict it.
        ok = bool(data.get("ready"))
        title = Text("✓ Ready" if ok else "✗ Not ready", style="bold green" if ok else "bold red")
        self.console.print(Panel(table, title=title, border_style="green" if ok else "red"))
        for hint in hints:
            self.console.print(Text(f"→ {hint}", style="yellow"))

    def _error(self, response: AppResponse) -> None:
        error_type = response.data.get("error_type", "operation_failed")
        lines = [Text(str(response.error or "The operation failed."), style="red")]
        body = Group(*lines, Text(f"Type: {error_type}", style="dim"))
        self.error_console.print(
            Panel(body, title="[bold red]✗ HarborRAG error[/]", border_style="red")
        )

    def _tree(self, title: str, data: Mapping[str, Any]) -> Tree:
        tree = Tree(Text(title, style="bold cyan"))
        self._tree_values(tree, data)
        return tree

    def _tree_values(self, tree: Tree, values: Mapping[str, Any]) -> None:
        for key, value in values.items():
            label = str(key).replace("_", " ").title()
            if isinstance(value, Mapping):
                branch = tree.add(Text(label, style="bold"))
                self._tree_values(branch, value)
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                branch = tree.add(Text(label, style="bold"))
                for item in value:
                    branch.add(text(item))
            else:
                tree.add(Text.assemble((f"{label}: ", "bold"), text(value)))
