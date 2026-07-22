"""Interactive Textual dashboard for a durable ingestion run."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Button, DataTable, Footer, Header, Label, ProgressBar, Static

from harborrag_app.cli.stages import build_stage_table
from harborrag_app.services.base import BaseAppService

type BindingSpec = Binding | tuple[str, str] | tuple[str, str, str]

_TERMINAL_STATUSES = frozenset({"cancelled", "completed", "failed"})
_STATUS_STYLES = {
    "pending": "yellow",
    "running": "bold cyan",
    "paused": "bold yellow",
    "cancelling": "bold yellow",
    "cancelled": "dim",
    "completed": "bold green",
    "failed": "bold red",
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


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    """Normalized dashboard view of the public ingestion status response."""

    run_id: str
    status: str
    current_partition: int | None
    paused: bool
    cancel_requested: bool
    progress: Mapping[str, Any]
    failed_artifacts: tuple[str, ...]
    quarantined_artifacts: tuple[str, ...]
    pending_resolutions: tuple[Mapping[str, Any], ...]

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        fallback_run_id: str,
    ) -> DashboardSnapshot:
        status = _mapping(payload.get("status"))
        progress = _mapping(payload.get("progress")) or _mapping(status.get("progress"))
        return cls(
            run_id=_string(status.get("run_id"), fallback=fallback_run_id),
            status=_string(status.get("status"), fallback="unknown").lower(),
            current_partition=_optional_integer(status.get("current_partition")),
            paused=bool(status.get("paused", False)),
            cancel_requested=bool(status.get("cancel_requested", False)),
            progress=progress,
            failed_artifacts=_strings(payload.get("failed_artifacts")),
            quarantined_artifacts=_strings(payload.get("quarantined_artifacts")),
            pending_resolutions=tuple(
                _mapping(item) for item in _sequence(payload.get("pending_resolutions"))
            ),
        )


class CancelConfirmation(ModalScreen[bool]):
    """Confirm a destructive dashboard cancellation action."""

    BINDINGS: ClassVar[list[BindingSpec]] = [
        Binding("escape", "dismiss(False)", "Keep running", show=False),
    ]

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    def compose(self) -> ComposeResult:
        with Container(id="cancel-dialog"):
            yield Label("Cancel ingestion?", id="cancel-title")
            yield Static(
                Text.assemble(
                    "Request graceful cancellation for ",
                    (self.run_id, "bold cyan"),
                    "? Reconciliation will still run.",
                ),
                id="cancel-copy",
            )
            with Horizontal(id="cancel-actions"):
                yield Button("Keep running", id="keep", variant="default")
                yield Button("Cancel run", id="confirm", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")


class IngestionDashboard(App[None]):
    """Live ingestion status, attention queue, and workflow controls."""

    TITLE = "HarborRAG"
    SUB_TITLE = "Ingestion control room"
    ENABLE_COMMAND_PALETTE = True
    BINDINGS: ClassVar[list[BindingSpec]] = [
        Binding("f", "refresh_status", "Refresh", priority=True),
        Binding("p", "pause_run", "Pause", priority=True),
        Binding("r", "resume_run", "Resume", priority=True),
        Binding("x", "cancel_run", "Cancel", priority=True),
        Binding("q", "quit", "Quit", priority=True),
    ]
    CSS = """
    Screen {
        background: #07111f;
        color: #dce7f5;
    }

    Header {
        background: #0d2035;
        color: #dff7ff;
    }

    Footer {
        background: #0d2035;
        color: #a9c4dc;
    }

    #dashboard {
        padding: 1 2;
        scrollbar-color: #29b6d8;
        scrollbar-background: #0b1727;
    }

    .card {
        background: #0b1727;
        border: round #275d8c;
        border-title-color: #65d9ef;
        padding: 0 1;
    }

    #overview {
        height: 5;
        margin-bottom: 1;
        content-align-vertical: middle;
    }

    #summary {
        height: 19;
        margin-bottom: 1;
    }

    #stages {
        width: 2fr;
        height: 100%;
        margin-right: 1;
    }

    #metrics {
        width: 1fr;
        height: 100%;
    }

    #progress-title {
        height: 2;
        color: #65d9ef;
        text-style: bold;
        content-align-vertical: middle;
    }

    #progress {
        height: 3;
        margin: 0 1 1 1;
    }

    Bar > .bar--bar {
        color: #29b6d8;
        background: #14283b;
    }

    Bar > .bar--complete {
        color: #4ee0a0;
    }

    PercentageStatus {
        color: #dff7ff;
        text-style: bold;
    }

    #counts {
        height: 1fr;
        padding: 0 1;
    }

    #attention {
        height: 12;
        min-height: 8;
        margin-bottom: 1;
        background: #0b1727;
        border: round #275d8c;
        border-title-color: #65d9ef;
    }

    #message {
        height: 3;
        margin-bottom: 1;
        color: #8ea9bf;
        content-align: left middle;
    }

    DataTable > .datatable--header {
        background: #15314d;
        color: #dff7ff;
        text-style: bold;
    }

    DataTable > .datatable--cursor {
        background: #1d5871;
        color: #ffffff;
    }

    CancelConfirmation {
        align: center middle;
        background: #020711 70%;
    }

    #cancel-dialog {
        width: 68;
        height: 12;
        padding: 1 2;
        background: #0b1727;
        border: heavy #e05a67;
    }

    #cancel-title {
        height: 2;
        color: #ff8b94;
        text-style: bold;
        content-align: center middle;
    }

    #cancel-copy {
        height: 4;
        content-align: center middle;
    }

    #cancel-actions {
        height: 3;
        align: center middle;
    }

    #cancel-actions Button {
        margin: 0 1;
    }
    """

    def __init__(
        self,
        run_id: str,
        service: BaseAppService,
        *,
        refresh_seconds: float = 1.0,
    ) -> None:
        super().__init__()
        self.run_id = run_id
        self.service = service
        self.refresh_seconds = refresh_seconds
        self.snapshot: DashboardSnapshot | None = None
        self.poll_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True, icon="⚓")
        with VerticalScroll(id="dashboard"):
            yield Static("Connecting to Temporal…", id="overview", classes="card")
            with Horizontal(id="summary"):
                yield Static(id="stages", classes="card")
                with Vertical(id="metrics", classes="card"):
                    yield Label("ARTIFACT PROGRESS", id="progress-title")
                    yield ProgressBar(
                        id="progress",
                        total=None,
                        show_eta=False,
                    )
                    yield Static(id="counts")
            yield DataTable(id="attention", cursor_type="none", zebra_stripes=True)
            yield Static(
                "Connecting to the workflow service…",
                id="message",
                classes="card",
            )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#overview", Static).border_title = "Run overview"
        self.query_one("#stages", Static).border_title = "Pipeline"
        self.query_one("#metrics", Vertical).border_title = "Progress"
        attention = self.query_one("#attention", DataTable)
        attention.border_title = "Attention queue"
        attention.add_column("State", width=14)
        attention.add_column("Artifact", width=30)
        attention.add_column("Detail")
        self.poll_timer = self.set_interval(self.refresh_seconds, self.load_status)
        self.load_status()

    @work(group="status", exclusive=True)
    async def load_status(self) -> None:
        """Fetch a consistent status snapshot without blocking UI events."""

        try:
            response = await self.service.ingestion_status(self.run_id)
        except Exception as exc:  # noqa: BLE001 - dashboard must remain interactive
            self._render_error(str(exc) or type(exc).__name__)
            return
        if not response.ok:
            self._render_error(response.error or "Unable to load ingestion status.")
            return
        snapshot = DashboardSnapshot.from_payload(
            response.data,
            fallback_run_id=self.run_id,
        )
        self.snapshot = snapshot
        self._render_snapshot(snapshot)

    def action_refresh_status(self) -> None:
        self.query_one("#message", Static).update("Refreshing workflow status…")
        self.load_status()

    def action_pause_run(self) -> None:
        self.control_run("pause")

    def action_resume_run(self) -> None:
        self.control_run("resume")

    def action_cancel_run(self) -> None:
        self.push_screen(CancelConfirmation(self.run_id), self._confirm_cancel)

    def _confirm_cancel(self, confirmed: bool | None) -> None:
        if confirmed:
            self.control_run("cancel")

    @work(group="control", exclusive=True)
    async def control_run(self, action: str) -> None:
        """Send a workflow control and immediately refresh its visible state."""

        try:
            response = await self.service.control_ingestion(self.run_id, action)
        except Exception as exc:  # noqa: BLE001 - dashboard must remain interactive
            self.notify(str(exc) or type(exc).__name__, title="Control failed", severity="error")
            return
        if response.ok:
            self.notify(
                f"{action.capitalize()} accepted for {self.run_id}",
                title="Workflow updated",
                severity="information",
            )
            self.load_status()
            return
        self.notify(
            response.error or f"Unable to {action} the run.",
            title="Control failed",
            severity="error",
        )

    def _render_snapshot(self, snapshot: DashboardSnapshot) -> None:
        self.sub_title = f"Ingestion control room · {snapshot.status.upper()}"
        self.query_one("#overview", Static).update(_overview(snapshot))
        self.query_one("#stages", Static).update(
            build_stage_table(
                snapshot.status,
                snapshot.progress,
                current_partition=snapshot.current_partition,
            )
        )
        discovered = _integer(snapshot.progress.get("discovered"))
        processed = _integer(snapshot.progress.get("processed"))
        progress = self.query_one("#progress", ProgressBar)
        progress.update(
            total=discovered or None,
            progress=min(processed, discovered) if discovered else 0,
        )
        self.query_one("#counts", Static).update(_metric_table(snapshot.progress))
        self._render_attention(snapshot)
        self.query_one("#message", Static).update(
            _message(snapshot, refresh_seconds=self.refresh_seconds)
        )
        if snapshot.status in _TERMINAL_STATUSES and self.poll_timer is not None:
            self.poll_timer.pause()

    def _render_attention(self, snapshot: DashboardSnapshot) -> None:
        table = self.query_one("#attention", DataTable)
        table.clear()
        rows = _attention_rows(snapshot)
        if not rows:
            table.add_row(
                Text("✓", style="green"),
                Text("No artifacts need attention", style="green"),
                Text("The run is progressing normally.", style="dim"),
            )
            return
        table.add_rows(rows)

    def _render_error(self, message: str) -> None:
        self.query_one("#overview", Static).update(
            Text.assemble(("✗ CONNECTION ERROR\n", "bold red"), message)
        )
        self.query_one("#message", Static).update(
            Text("Press F to retry or Q to leave the dashboard.", style="yellow")
        )


def _overview(snapshot: DashboardSnapshot) -> Text:
    value = Text()
    value.append("RUN  ", style="dim")
    value.append(snapshot.run_id, style="bold cyan")
    value.append("    STATUS  ", style="dim")
    value.append(f"● {snapshot.status.upper()}", style=_STATUS_STYLES.get(snapshot.status, "white"))
    value.append("    PARTITION  ", style="dim")
    value.append(
        "—" if snapshot.current_partition is None else str(snapshot.current_partition),
        style="bold",
    )
    value.append("\n")
    value.append("PAUSED  ", style="dim")
    value.append("YES" if snapshot.paused else "NO", style="yellow" if snapshot.paused else "green")
    value.append("    CANCELLATION REQUESTED  ", style="dim")
    value.append(
        "YES" if snapshot.cancel_requested else "NO",
        style="yellow" if snapshot.cancel_requested else "green",
    )
    return value


def _metric_table(progress: Mapping[str, Any]) -> Table:
    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column(ratio=1)
    table.add_column(justify="right")
    for label, key, style in _METRICS:
        table.add_row(
            Text(label, style="dim"),
            Text(f"{_integer(progress.get(key)):,}", style=f"bold {style}"),
        )
    return table


def _attention_rows(snapshot: DashboardSnapshot) -> list[tuple[Text, Text, Text]]:
    rows: list[tuple[Text, Text, Text]] = []
    rows.extend(
        (Text("FAILED", style="bold red"), Text(artifact), Text("Retry from CLI", style="dim"))
        for artifact in snapshot.failed_artifacts
    )
    rows.extend(
        (
            Text("QUARANTINED", style="bold magenta"),
            Text(artifact),
            Text("Review validation result", style="dim"),
        )
        for artifact in snapshot.quarantined_artifacts
    )
    for pending in snapshot.pending_resolutions:
        detail = _string(pending.get("reason"), fallback="Resolution required")
        resume_stage = _string(pending.get("resume_stage"), fallback="unknown")
        rows.append(
            (
                Text("WAITING", style="bold yellow"),
                Text(_string(pending.get("artifact_id"), fallback="—")),
                Text(f"{detail} · resume {resume_stage}", style="yellow"),
            )
        )
    return rows


def _message(snapshot: DashboardSnapshot, *, refresh_seconds: float) -> Text:
    if snapshot.status in _TERMINAL_STATUSES:
        return Text(
            "Terminal state reached. Automatic polling is paused; press F to refresh.",
            style="dim",
        )
    return Text.assemble(
        ("LIVE  ", "bold cyan"),
        f"Polling Temporal every {refresh_seconds:g}s. ",
        "Use the footer shortcuts to control the run.",
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else ()


def _strings(value: Any) -> tuple[str, ...]:
    return tuple(str(item) for item in _sequence(value))


def _string(value: Any, *, fallback: str) -> str:
    return str(value) if value is not None and str(value) else fallback


def _integer(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _optional_integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
