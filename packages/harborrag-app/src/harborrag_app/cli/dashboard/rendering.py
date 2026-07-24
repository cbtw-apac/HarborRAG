from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from rich.table import Table
from rich.text import Text

from .schemas import DashboardSnapshot, as_integer, as_string

TERMINAL_STATUSES = frozenset({"cancelled", "completed", "failed"})
STATUS_STYLES = {
    "pending": "yellow",
    "running": "bold cyan",
    "paused": "bold yellow",
    "cancelling": "bold yellow",
    "cancelled": "dim",
    "completed": "bold green",
    "failed": "bold red",
}
METRICS = (
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


def overview(snapshot: DashboardSnapshot) -> Text:
    value = Text()
    value.append("RUN  ", style="dim")
    value.append(snapshot.run_id, style="bold cyan")
    value.append("    STATUS  ", style="dim")
    value.append(
        f"● {snapshot.status.upper()}",
        style=STATUS_STYLES.get(snapshot.status, "white"),
    )
    value.append("    PARTITION  ", style="dim")
    value.append(
        "—" if snapshot.current_partition is None else str(snapshot.current_partition),
        style="bold",
    )
    value.append("\n")
    value.append("PAUSED  ", style="dim")
    value.append(
        "YES" if snapshot.paused else "NO",
        style="yellow" if snapshot.paused else "green",
    )
    value.append("    CANCELLATION REQUESTED  ", style="dim")
    value.append(
        "YES" if snapshot.cancel_requested else "NO",
        style="yellow" if snapshot.cancel_requested else "green",
    )
    return value


def metric_table(progress: Mapping[str, Any]) -> Table:
    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column(ratio=1)
    table.add_column(justify="right")
    for label, key, style in METRICS:
        table.add_row(
            Text(label, style="dim"),
            Text(f"{as_integer(progress.get(key)):,}", style=f"bold {style}"),
        )
    return table


def attention_rows(
    snapshot: DashboardSnapshot,
) -> list[tuple[Text, Text, Text]]:
    rows: list[tuple[Text, Text, Text]] = []
    rows.extend(
        (
            Text("FAILED", style="bold red"),
            Text(artifact),
            Text("Retry from CLI", style="dim"),
        )
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
        detail = as_string(pending.get("reason"), fallback="Resolution required")
        resume_stage = as_string(pending.get("resume_stage"), fallback="unknown")
        rows.append(
            (
                Text("WAITING", style="bold yellow"),
                Text(as_string(pending.get("artifact_id"), fallback="—")),
                Text(f"{detail} · resume {resume_stage}", style="yellow"),
            )
        )
    return rows


def message(snapshot: DashboardSnapshot, *, refresh_seconds: float) -> Text:
    if snapshot.status in TERMINAL_STATUSES:
        return Text(
            "Terminal state reached. Automatic polling is paused; press F to refresh.",
            style="dim",
        )
    return Text.assemble(
        ("LIVE  ", "bold cyan"),
        f"Polling Temporal every {refresh_seconds:g}s. ",
        "Use the footer shortcuts to control the run.",
    )
