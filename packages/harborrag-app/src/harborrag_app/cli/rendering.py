"""Rich presentation for operator-facing CLI responses."""

from __future__ import annotations

from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from harborrag_app.cli.rendering_values import (
    STATUS_STYLES,
    boolean,
    details_table,
    integer,
    mapping,
    optional_integer,
    sequence,
    status_text,
    text,
)
from harborrag_app.cli.stages import build_stage_table, headline_status
from harborrag_app.workflow_control import AppResponse

# Metric cells are laid out in a fixed grid; the width is a presentation choice, not data.
_METRIC_COLUMNS = 3
_PROGRESS_BAR_WIDTH = 40

_METRICS = (
    ("Discovered", "discovered", "cyan"),
    ("Processed", "processed", "blue"),
    ("Published", "published", "green"),
    ("Unchanged", "unchanged", "dim"),
    ("Failed", "failed", "red"),
    ("Batches", "completed_batches", "cyan"),
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
        """Select the view for a command/action pair, or None for the generic tree."""

        by_command = {"doctor": self._doctor, "chat": self._chat, "status": self._status}
        by_action = {
            "status": self._status,
            "start": self._started,
            "wait": self._result,
            "pause": self._control,
            "resume": self._control,
            "cancel": self._control,
        }
        return by_command.get(command) or by_action.get(action or "")

    def _chat(self, data: Mapping[str, Any]) -> None:
        message = mapping(data.get("message"))
        content = text(message.get("content", ""))
        model = text(data.get("model", "unknown"))
        provider = text(data.get("provider", "unknown"))
        title = Text.assemble(("HarborChat", "bold cyan"), " ", model, " ", provider)
        self.console.print(Panel(content, title=title, border_style="cyan"))

    def _doctor(self, data: Mapping[str, Any]) -> None:
        runtime = mapping(data.get("runtime"))
        ready = bool(runtime.get("ready"))
        table = details_table()
        table.add_row("Provider", text(runtime.get("provider", "unknown")))
        table.add_row("Target", text(runtime.get("target", "—")))
        table.add_row("Namespace", text(runtime.get("namespace", "—")))
        title = Text("✓ Runtime ready" if ready else "Runtime unavailable")
        title.stylize("bold green" if ready else "bold red")
        self.console.print(Panel(table, title=title, border_style="green" if ready else "red"))

    def _started(self, data: Mapping[str, Any]) -> None:
        run = mapping(data.get("run"))
        workflow = mapping(data.get("workflow"))
        table = details_table()
        rows = (
            ("Run", run.get("run_id")),
            ("Tenant", run.get("tenant_id")),
            ("Connector", run.get("connector_name")),
            ("Type", run.get("connector_type")),
            ("Connection", run.get("connection_id")),
            ("Scope", run.get("source_scope_id")),
            ("Workflow", workflow.get("workflow_id")),
        )
        for label, value in rows:
            table.add_row(label, text(value, style="cyan" if label == "Run" else None))
        self.console.print(Panel(table, title="[bold green]✓ Ingestion started[/]"))
        if "result" in data:
            self.console.print(self._summary_panel(mapping(data["result"])))
        else:
            self.console.print(build_stage_table("pending", {}))

    def _status(self, data: Mapping[str, Any]) -> None:
        status = mapping(data.get("status"))
        progress = mapping(data.get("progress"))
        if not progress:
            progress = mapping(status.get("progress"))
        value = str(status.get("status", "unknown")).lower()
        header = details_table()
        header.add_row("Run", text(status.get("run_id", "—"), style="cyan"))
        header.add_row("Status", status_text(value))
        # Temporal's own execution status is the only field that turns terminal
        # when a workflow crashes, so show it next to the workflow's self-report
        # rather than leaving "running" as the last word on a dead run.
        execution = str(data.get("execution_status", "")).lower()
        if execution:
            header.add_row("Execution", status_text(execution))
        headline = headline_status(value, execution)
        header.add_row("Partition", text(status.get("current_partition", "—")))
        header.add_row("Paused", boolean(status.get("paused", False)))
        header.add_row(
            "Cancellation requested",
            boolean(status.get("cancel_requested", False)),
        )
        title = Text(
            f"Ingestion {headline}",
            style=f"bold {STATUS_STYLES.get(headline, 'white')}",
        )
        self.console.print(Panel(header, title=title))
        self.console.print(
            build_stage_table(
                headline,
                progress,
                current_partition=optional_integer(status.get("current_partition")),
            )
        )
        self.console.print(self._progress(progress))
        self._artifact_sections(data)

    def _result(self, data: Mapping[str, Any]) -> None:
        self.console.print(self._summary_panel(mapping(data.get("result"))))

    def _control(self, data: Mapping[str, Any]) -> None:
        action = str(data.get("action", "updated"))
        run_id = str(data.get("run_id", "—"))
        message = Text()
        message.append(f"{action.capitalize()} accepted", style="bold green")
        message.append(f" for {run_id}")
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
        details = details_table()
        details.add_row(
            "Run",
            text(summary.get("task_id", summary.get("run_id", "—")), style="cyan"),
        )
        details.add_row("Scan", text(summary.get("scan_id", "—")))
        details.add_row("Status", status_text(status))
        progress = mapping(summary.get("progress"))
        if not progress:
            progress = {
                key: summary[key]
                for key in (
                    "discovered",
                    "published",
                    "unchanged",
                    "failed",
                )
                if key in summary
            }
            progress = {
                **progress,
                "processed": sum(
                    integer(progress.get(key)) for key in ("published", "unchanged", "failed")
                ),
            }
        title = Text(f"Ingestion {status}", style=f"bold {STATUS_STYLES.get(status, 'white')}")
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
        discovered = integer(progress.get("discovered"))
        processed = integer(progress.get("processed"))
        grid = Table.grid(expand=True)
        grid.add_column(ratio=1)
        grid.add_column(justify="right")
        if discovered:
            grid.add_row(
                ProgressBar(
                    total=discovered,
                    completed=min(processed, discovered),
                    width=_PROGRESS_BAR_WIDTH,
                ),
                Text(f"{processed:,}/{discovered:,}", style="bold"),
            )
        else:
            grid.add_row(Text("Waiting for discovery", style="dim"), Text("0/0", style="dim"))

        metrics = Table(box=box.SIMPLE, expand=True, show_header=False, pad_edge=False)
        for _ in range(_METRIC_COLUMNS):
            metrics.add_column(ratio=1)
        cells: list[RenderableType] = []
        for label, key, style in _METRICS:
            cells.append(Text(f"{label}  {integer(progress.get(key)):,}", style=style))
        while len(cells) % _METRIC_COLUMNS:
            cells.append(Text(""))
        for offset in range(0, len(cells), _METRIC_COLUMNS):
            metrics.add_row(*cells[offset : offset + _METRIC_COLUMNS])
        return Group(grid, metrics)

    def _artifact_sections(self, data: Mapping[str, Any]) -> None:
        sections = (
            ("Failed artifacts", data.get("failed_artifacts"), "red"),
            ("Quarantined artifacts", data.get("quarantined_artifacts"), "magenta"),
        )
        for title, values, style in sections:
            items = sequence(values)
            if items:
                self.console.print(
                    Panel(
                        Text("\n".join(str(item) for item in items)),
                        title=title,
                        border_style=style,
                    )
                )
        pending = sequence(data.get("pending_resolutions"))
        if pending:
            table = Table(title="Pending resolutions", box=box.SIMPLE)
            table.add_column("Artifact", style="cyan")
            table.add_column("Reason")
            table.add_column("Resume stage")
            for value in pending:
                item = mapping(value)
                table.add_row(
                    text(item.get("artifact_id", "—")),
                    text(item.get("reason", "—")),
                    text(item.get("resume_stage", "—")),
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
                    branch.add(text(item))
            else:
                tree.add(Text.assemble((f"{label}: ", "bold"), text(value)))
