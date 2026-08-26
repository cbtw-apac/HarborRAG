"""A crashed workflow must not keep reporting itself as running.

``get_status`` is answered from state the workflow tracks about itself, so a run
whose workflow raised never records its own failure and replies "running"
forever. These tests pin the three places that now reconcile that against
Temporal's server-side execution status.
"""

from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from harborrag_app.cli.rendering import CliRenderer
from harborrag_app.cli.stages import headline_status
from harborrag_app.workflow_control import AppResponse


@pytest.mark.parametrize(
    ("execution_status", "expected"),
    [
        ("failed", "failed"),
        ("terminated", "failed"),
        ("timed_out", "failed"),
        ("canceled", "cancelled"),
        ("completed", "completed"),
    ],
)
def test_terminal_execution_overrides_a_stale_running_workflow(
    execution_status: str,
    expected: str,
) -> None:
    assert headline_status("running", execution_status) == expected


@pytest.mark.parametrize("workflow_status", ["running", "paused", "cancelling"])
def test_live_execution_keeps_the_workflow_view(workflow_status: str) -> None:
    """Only the workflow view distinguishes paused and cancelling from running."""

    assert headline_status(workflow_status, "running") == workflow_status


def test_missing_execution_status_leaves_the_workflow_view_untouched() -> None:
    assert headline_status("running", "") == "running"


def test_graceful_cancellation_is_not_relabelled_as_completion() -> None:
    assert headline_status("cancelled", "completed") == "cancelled"


def test_status_renderer_shows_both_views_and_headlines_the_truth() -> None:
    """The operator sees the disagreement, not just one side of it."""

    output = StringIO()
    renderer = CliRenderer(
        console=Console(file=output, force_terminal=False, width=100),
        error_console=Console(file=StringIO(), force_terminal=False, width=100),
    )
    response = AppResponse(
        True,
        {
            "status": {"run_id": "run-1", "status": "running", "progress": {}},
            "execution_status": "failed",
            "progress": {},
        },
    )

    renderer.render(response, command="ingest", action="status")

    rendered = output.getvalue()
    assert "Ingestion failed" in rendered
    assert "RUNNING" in rendered
    assert "FAILED" in rendered
