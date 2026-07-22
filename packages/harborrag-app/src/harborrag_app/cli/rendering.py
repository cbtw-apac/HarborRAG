"""Rich presentation for operator-facing CLI responses."""

from __future__ import annotations

from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from harborrag_app.cli.stages import build_stage_table
from harborrag_app.services.base import AppResponse

_STATUS_STYLES = {
    "pending": "yellow",
    "running": "cyan",
    "paused": "yellow",
    "cancelling": "yellow",
    "cancelled": "dim",
    "completed": "green",
    "failed": "red",
}

_METRICS = (
    ("Discovered", "discovered", "cyan"),
    ("Processed", "processed", "blue"),
    ("Succeeded", "succeeded", "green"),
    ("Unchanged", "unchanged", "dim"),
    ("Skipped", "skipped", "yellow"),
    ("Failed", "failed", "red"),
    ("Quarantined", "quarantined", "magenta"),
    ("Cancelled", "cancelled", "dim"),
    ("Partitions", "partitions", "cyan"),
)


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
        if command == "doctor":
            self._doctor(response.data)
        elif command == "status" or action == "status":
            self._status(response.data)
        elif action == "start":
            self._started(response.data)
        elif action == "wait":
            self._result(response.data)
        elif action in {"pause", "resume", "cancel", "retry"}:
            self._control(response.data)
        else:
            self.console.print(self._tree("Result", response.data))

    def _doctor(self, data: Mapping[str, Any]) -> None:
        runtime = self._mapping(data.get("runtime"))
        ready = bool(runtime.get("ready"))
        table = self._details_table()
        table.add_row("Provider", self._text(runtime.get("provider", "unknown")))
        table.add_row("Target", self._text(runtime.get("target", "—")))
        table.add_row("Namespace", self._text(runtime.get("namespace", "—")))
        title = Text("✓ Runtime ready" if ready else "Runtime unavailable")
        title.stylize("bold green" if ready else "bold red")
        self.console.print(Panel(table, title=title, border_style="green" if ready else "red"))

    def _started(self, data: Mapping[str, Any]) -> None:
        run = self._mapping(data.get("run"))
        workflow = self._mapping(data.get("workflow"))
        table = self._details_table()
        rows = (
            ("Run", run.get("run_id")),
            ("Tenant", run.get("tenant_id")),
            ("Connector", run.get("connector_name")),
            ("Manifest", run.get("manifest_id")),
            ("Generation", run.get("generation_id")),
            ("Workflow", workflow.get("workflow_id")),
        )
        for label, value in rows:
            table.add_row(label, self._text(value, style="cyan" if label == "Run" else None))
        self.console.print(Panel(table, title="[bold green]✓ Ingestion started[/]"))
        if "result" in data:
            self.console.print(self._summary_panel(self._mapping(data["result"])))
        else:
            self.console.print(build_stage_table("pending", {}))

    def _status(self, data: Mapping[str, Any]) -> None:
        status = self._mapping(data.get("status"))
        progress = self._mapping(data.get("progress"))
        if not progress:
            progress = self._mapping(status.get("progress"))
        value = str(status.get("status", "unknown")).lower()
        header = self._details_table()
        header.add_row("Run", self._text(status.get("run_id", "—"), style="cyan"))
        header.add_row("Status", self._status_text(value))
        header.add_row("Partition", self._text(status.get("current_partition", "—")))
        header.add_row("Paused", self._boolean(status.get("paused", False)))
        header.add_row(
            "Cancellation requested",
            self._boolean(status.get("cancel_requested", False)),
        )
        title = Text(f"Ingestion {value}", style=f"bold {_STATUS_STYLES.get(value, 'white')}")
        self.console.print(Panel(header, title=title))
        self.console.print(
            build_stage_table(
                value,
                progress,
                current_partition=self._optional_integer(status.get("current_partition")),
            )
        )
        self.console.print(self._progress(progress))
        self._artifact_sections(data)

    def _result(self, data: Mapping[str, Any]) -> None:
        self.console.print(self._summary_panel(self._mapping(data.get("result"))))

    def _control(self, data: Mapping[str, Any]) -> None:
        action = str(data.get("action", "updated"))
        run_id = str(data.get("run_id", "—"))
        artifacts = self._sequence(data.get("artifact_ids"))
        message = Text()
        message.append(f"{action.capitalize()} accepted", style="bold green")
        message.append(f" for {run_id}")
        if artifacts:
            message.append(f" ({len(artifacts)} artifact{'s' if len(artifacts) != 1 else ''})")
        self.console.print(Panel(message, border_style="green"))

    def _error(self, response: AppResponse) -> None:
        error_type = response.data.get("error_type", "operation_failed")
        body = Group(
            Text(str(response.error or "The operation failed."), style="red"),
            Text(f"Type: {error_type}", style="dim"),
        )
        self.error_console.print(
            Panel(body, title="[bold red]✗ HarborRAG error[/]", border_style="red")
        )

    def _summary_panel(self, summary: Mapping[str, Any]) -> Panel:
        status = str(summary.get("status", "unknown")).lower()
        details = self._details_table()
        details.add_row("Run", self._text(summary.get("run_id", "—"), style="cyan"))
        details.add_row("Manifest", self._text(summary.get("manifest_id", "—")))
        details.add_row("Status", self._status_text(status))
        details.add_row(
            "Reconciliation",
            self._text(summary.get("reconciliation_ref") or "—"),
        )
        progress = self._mapping(summary.get("progress"))
        title = Text(f"Ingestion {status}", style=f"bold {_STATUS_STYLES.get(status, 'white')}")
        return Panel(
            Group(
                details,
                Text(""),
                build_stage_table(status, progress),
                Text(""),
                self._progress(progress),
            ),
            title=title,
        )

    def _progress(self, progress: Mapping[str, Any]) -> RenderableType:
        discovered = self._integer(progress.get("discovered"))
        processed = self._integer(progress.get("processed"))
        grid = Table.grid(expand=True)
        grid.add_column(ratio=1)
        grid.add_column(justify="right")
        if discovered:
            grid.add_row(
                ProgressBar(total=discovered, completed=min(processed, discovered), width=40),
                Text(f"{processed:,}/{discovered:,}", style="bold"),
            )
        else:
            grid.add_row(Text("Waiting for discovery", style="dim"), Text("0/0", style="dim"))

        metrics = Table(box=box.SIMPLE, expand=True, show_header=False, pad_edge=False)
        for _ in range(3):
            metrics.add_column(ratio=1)
        cells: list[RenderableType] = []
        for label, key, style in _METRICS:
            cells.append(Text(f"{label}  {self._integer(progress.get(key)):,}", style=style))
        while len(cells) % 3:
            cells.append(Text(""))
        for offset in range(0, len(cells), 3):
            metrics.add_row(*cells[offset : offset + 3])
        return Group(grid, metrics)

    def _artifact_sections(self, data: Mapping[str, Any]) -> None:
        sections = (
            ("Failed artifacts", data.get("failed_artifacts"), "red"),
            ("Quarantined artifacts", data.get("quarantined_artifacts"), "magenta"),
        )
        for title, values, style in sections:
            items = self._sequence(values)
            if items:
                self.console.print(
                    Panel(
                        Text("\n".join(str(item) for item in items)),
                        title=title,
                        border_style=style,
                    )
                )
        pending = self._sequence(data.get("pending_resolutions"))
        if pending:
            table = Table(title="Pending resolutions", box=box.SIMPLE)
            table.add_column("Artifact", style="cyan")
            table.add_column("Reason")
            table.add_column("Resume stage")
            for value in pending:
                item = self._mapping(value)
                table.add_row(
                    self._text(item.get("artifact_id", "—")),
                    self._text(item.get("reason", "—")),
                    self._text(item.get("resume_stage", "—")),
                )
            self.console.print(table)

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
                    branch.add(self._text(item))
            else:
                tree.add(Text.assemble((f"{label}: ", "bold"), self._text(value)))

    @staticmethod
    def _details_table() -> Table:
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold", justify="right")
        table.add_column()
        return table

    @staticmethod
    def _mapping(value: Any) -> Mapping[str, Any]:
        return value if isinstance(value, Mapping) else {}

    @staticmethod
    def _sequence(value: Any) -> Sequence[Any]:
        return value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else ()

    @staticmethod
    def _integer(value: Any) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    @staticmethod
    def _optional_integer(value: Any) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @staticmethod
    def _text(value: Any, *, style: str | None = None) -> Text:
        text = "—" if value is None or value == "" else str(value)
        return Text(text, style=style) if style else Text(text)

    @staticmethod
    def _boolean(value: Any) -> Text:
        enabled = bool(value)
        return Text("yes" if enabled else "no", style="yellow" if enabled else "dim")

    @staticmethod
    def _status_text(value: str) -> Text:
        return Text(value.upper(), style=f"bold {_STATUS_STYLES.get(value, 'white')}")
