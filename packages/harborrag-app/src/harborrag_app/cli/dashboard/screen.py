from __future__ import annotations

import logging
from typing import ClassVar

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.timer import Timer
from textual.widgets import DataTable, Footer, Header, Label, ProgressBar, Static

from harborrag_app.cli.stages import build_stage_table
from harborrag_app.workflow_control import BaseAppService
from harborrag_app.workflow_control.errors import public_error_message

from .dialogs import CancelConfirmation
from .rendering import (
    TERMINAL_STATUSES,
    attention_rows,
    message,
    metric_table,
    overview,
)
from .schemas import DashboardSnapshot, as_integer
from .styles import DASHBOARD_CSS

logger = logging.getLogger("harborrag.app.cli.dashboard")

type BindingSpec = Binding | tuple[str, str] | tuple[str, str, str]


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
    CSS = DASHBOARD_CSS

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
            logger.error(
                "Dashboard status refresh failed run_id=%s error_type=%s",
                self.run_id,
                type(exc).__name__,
            )
            self._render_error(public_error_message(exc))
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
            logger.error(
                "Dashboard workflow control failed run_id=%s action=%s error_type=%s",
                self.run_id,
                action,
                type(exc).__name__,
            )
            self.notify(
                public_error_message(exc),
                title="Control failed",
                severity="error",
            )
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
        self.query_one("#overview", Static).update(overview(snapshot))
        self.query_one("#stages", Static).update(
            build_stage_table(
                snapshot.status,
                snapshot.progress,
                current_partition=snapshot.current_partition,
            )
        )
        discovered = as_integer(snapshot.progress.get("discovered"))
        processed = as_integer(snapshot.progress.get("processed"))
        progress = self.query_one("#progress", ProgressBar)
        progress.update(
            total=discovered or None,
            progress=min(processed, discovered) if discovered else 0,
        )
        self.query_one("#counts", Static).update(metric_table(snapshot.progress))
        self._render_attention(snapshot)
        self.query_one("#message", Static).update(
            message(snapshot, refresh_seconds=self.refresh_seconds)
        )
        if snapshot.status in TERMINAL_STATUSES and self.poll_timer is not None:
            self.poll_timer.pause()

    def _render_attention(self, snapshot: DashboardSnapshot) -> None:
        table = self.query_one("#attention", DataTable)
        table.clear()
        rows = attention_rows(snapshot)
        if not rows:
            table.add_row(
                Text("✓", style="green"),
                Text("No artifacts need attention", style="green"),
                Text("The run is progressing normally.", style="dim"),
            )
            return
        table.add_rows(rows)

    def _render_error(self, message_text: str) -> None:
        self.query_one("#overview", Static).update(
            Text.assemble(("✗ CONNECTION ERROR\n", "bold red"), message_text)
        )
        self.query_one("#message", Static).update(
            Text("Press F to retry or Q to leave the dashboard.", style="yellow")
        )
