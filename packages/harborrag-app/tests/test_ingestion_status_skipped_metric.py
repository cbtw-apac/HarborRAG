"""``ingest status`` shows the discovery-time skips the run reported."""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from harborrag_app.cli.rendering import CliRenderer
from harborrag_app.workflow_control import AppResponse


def test_status_renderer_shows_skipped_count_from_progress() -> None:
    output = StringIO()
    renderer = CliRenderer(
        console=Console(file=output, force_terminal=False, width=120),
        error_console=Console(file=StringIO(), force_terminal=False, width=120),
    )
    response = AppResponse(
        True,
        {
            "status": {"run_id": "run-1", "status": "completed", "progress": {}},
            "execution_status": "completed",
            "progress": {
                "discovered": 3,
                "processed": 3,
                "succeeded": 3,
                "published": 3,
                "unchanged": 0,
                "failed": 0,
                "skipped": 2,
                "completed_batches": 1,
            },
        },
    )

    renderer.render(response, command="ingest", action="status")

    rendered = output.getvalue()
    assert "Skipped" in rendered
    skipped_row = next(line for line in rendered.splitlines() if "Skipped" in line)
    assert "2" in skipped_row
