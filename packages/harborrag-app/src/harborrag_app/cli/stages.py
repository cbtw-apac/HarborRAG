"""Rich visualization of the durable ingestion stage sequence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from rich import box
from rich.table import Table
from rich.text import Text


@dataclass(frozen=True, slots=True)
class StageView:
    """One operator-facing stage and its aggregate run state."""

    name: str
    description: str
    state: str


_STAGES = (
    ("Discover", "Enumerate source artifacts"),
    ("Preflight", "Check revisions and generation fencing"),
    ("Fetch", "Load the immutable source snapshot"),
    ("Parse", "Create structured document elements"),
    ("Chunk", "Build and persist canonical chunks"),
    ("Index", "Stage vector and graph records"),
    ("Validate", "Read back and validate staged indexes"),
    ("Finalize", "Activate the validated generation"),
    ("Reconcile", "Persist the terminal run outcome"),
)

_STATE_PRESENTATION = {
    "unknown": ("?", "dim"),
    "queued": ("○", "dim"),
    "active": ("●", "bold cyan"),
    "in_flight": ("↻", "cyan"),
    "complete": ("✓", "green"),
    "closed": ("■", "dim"),
    "paused": ("Ⅱ", "yellow"),
    "stopped": ("■", "dim"),
}

_RUN_STATUSES = frozenset(
    {"pending", "running", "paused", "cancelling", "cancelled", "completed", "failed"}
)

# Temporal execution statuses that settle a run, mapped onto the run vocabulary
# above. Temporal uses American spelling and reports outcomes ("terminated",
# "timed_out") that the workflow has no opportunity to record about itself.
_TERMINAL_EXECUTION_STATUSES = {
    "failed": "failed",
    "terminated": "failed",
    "timed_out": "failed",
    "canceled": "cancelled",
    "completed": "completed",
}


def headline_status(workflow_status: str, execution_status: str) -> str:
    """Prefer Temporal's verdict once the execution has actually finished.

    A workflow that crashed keeps self-reporting "running", so presenting only
    its own view would headline a dead run as live and draw an active stage
    table. Non-terminal executions fall through to the workflow status, which
    is the only one that distinguishes paused and cancelling from running.
    """

    if workflow_status in {"cancelled", "completed", "failed"}:
        return workflow_status
    terminal = _TERMINAL_EXECUTION_STATUSES.get(execution_status)
    if terminal is None or workflow_status == terminal:
        return workflow_status
    return terminal


def build_stage_table(
    status: str,
    progress: Mapping[str, Any],
    *,
    current_partition: int | None = None,
) -> Table:
    """Build a stage table from the aggregate workflow status contract.

    Artifact stages execute concurrently, so an active batch intentionally marks
    every artifact stage as in flight instead of claiming a single global stage.
    """

    table = Table(
        title="Ingestion stages",
        box=box.SIMPLE,
        expand=True,
        pad_edge=False,
    )
    table.add_column("", width=2, justify="center")
    table.add_column("Stage", style="bold", no_wrap=True)
    table.add_column("State", no_wrap=True)
    table.add_column("Responsibility", ratio=1)
    for view in stage_views(
        status,
        progress,
        current_partition=current_partition,
    ):
        symbol, style = _STATE_PRESENTATION[view.state]
        table.add_row(
            Text(symbol, style=style),
            Text(view.name),
            Text(view.state.replace("_", " "), style=style),
            Text(view.description, style="dim"),
        )
    return table


def stage_views(
    status: str,
    progress: Mapping[str, Any],
    *,
    current_partition: int | None = None,
) -> tuple[StageView, ...]:
    """Derive honest aggregate stage states from the public run progress."""

    status = status.lower()
    discovered = _count(progress.get("discovered"))
    processed = _count(progress.get("processed"))
    if status not in _RUN_STATUSES:
        states = ("unknown",) * len(_STAGES)
    elif status == "completed":
        states = ("complete",) * len(_STAGES)
    elif status == "failed":
        # The public status exposes failed artifacts, but not the stage where
        # each artifact stopped. Close the artifact lanes without falsely
        # attributing the run failure to every stage.
        states = (
            "complete" if discovered else "closed",
            *(("closed",) * 7),
            "complete",
        )
    elif status == "cancelled":
        states = (
            "complete" if discovered else "stopped",
            *(("stopped",) * 7),
            "complete",
        )
    elif status == "paused":
        states = (
            "complete" if discovered else "paused",
            *(("paused",) * 7),
            "queued",
        )
    elif status == "pending":
        states = ("queued",) * len(_STAGES)
    elif discovered == 0:
        states = ("active", *(("queued",) * 8))
    elif processed < discovered or current_partition is not None:
        states = ("complete", *(("in_flight",) * 7), "queued")
    elif status == "cancelling":
        states = ("complete", *(("stopped",) * 7), "active")
    else:
        # Between partition batches the workflow may discover another page or
        # begin reconciliation. "active" on both coordination boundaries is
        # more accurate than inventing an artifact-level current stage.
        states = ("active", *(("complete",) * 7), "active")
    return tuple(
        StageView(name=name, description=description, state=state)
        for (name, description), state in zip(_STAGES, states, strict=True)
    )


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
