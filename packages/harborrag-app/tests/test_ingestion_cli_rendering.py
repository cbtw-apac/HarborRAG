from __future__ import annotations

from io import StringIO

from rich.console import Console

from harborrag_app.cli.rendering import CliRenderer
from harborrag_app.cli.stages import stage_views
from harborrag_app.workflow_control import AppResponse


def make_renderer() -> tuple[CliRenderer, StringIO, StringIO]:
    output = StringIO()
    errors = StringIO()
    renderer = CliRenderer(
        console=Console(file=output, force_terminal=False, width=100),
        error_console=Console(file=errors, force_terminal=False, width=100),
    )
    return renderer, output, errors


def test_status_renderer_shows_progress_and_actionable_artifacts() -> None:
    renderer, output, _ = make_renderer()
    response = AppResponse(
        True,
        {
            "status": {
                "run_id": "run-1",
                "status": "running",
                "current_partition": 2,
                "paused": False,
                "cancel_requested": False,
            },
            "progress": {
                "discovered": 10,
                "processed": 7,
                "succeeded": 5,
                "unchanged": 1,
                "failed": 1,
                "partitions": 2,
            },
            "failed_artifacts": ["document-7"],
            "quarantined_artifacts": [],
            "pending_resolutions": [],
        },
    )

    renderer.render(response, command="ingest", action="status")

    rendered = output.getvalue()
    assert "Ingestion running" in rendered
    assert "run-1" in rendered
    assert "Ingestion stages" in rendered
    assert "Preflight" in rendered
    assert "in flight" in rendered
    assert "Reconcile" in rendered
    assert "queued" in rendered
    assert "Processed  7" in rendered
    assert "Failed artifacts" in rendered
    assert "document-7" in rendered


def test_start_renderer_hides_internal_options_and_shows_operator_ids() -> None:
    renderer, output, _ = make_renderer()
    response = AppResponse(
        True,
        {
            "run": {
                "run_id": "run-1",
                "tenant_id": "tenant-1",
                "connector_name": "local-docs",
                "connector_type": "local",
                "connection_id": "local-docs",
                "source_scope_id": "docs",
            },
            "workflow": {"workflow_id": "ingestion/run-1"},
        },
    )

    renderer.render(response, command="ingest", action="start")

    rendered = output.getvalue()
    assert "Ingestion started" in rendered
    assert "tenant-1" in rendered
    assert "local-docs" in rendered
    assert "ingestion/run-1" in rendered
    assert "Ingestion stages" in rendered
    assert "Discover" in rendered
    assert "queued" in rendered
    assert "not-rendered" not in rendered


def test_completed_result_shows_every_stage_as_complete() -> None:
    renderer, output, _ = make_renderer()
    response = AppResponse(
        True,
        {
            "result": {
                "task_id": "run-1",
                "scan_id": "scan-1",
                "status": "COMPLETED",
                "discovered": 3,
                "published": 3,
                "unchanged": 0,
                "failed": 0,
            }
        },
    )

    renderer.render(response, command="ingest", action="wait")

    rendered = output.getvalue()
    assert "Ingestion completed" in rendered
    assert "Finalize" in rendered
    assert "Reconcile" in rendered
    assert "in flight" not in rendered


def test_stage_views_do_not_invent_one_global_artifact_stage() -> None:
    views = stage_views(
        "running",
        {"discovered": 10, "processed": 4},
        current_partition=2,
    )

    assert views[0].state == "complete"
    assert {view.state for view in views[1:8]} == {"in_flight"}
    assert views[-1].state == "queued"


def test_pending_stage_views_are_all_queued() -> None:
    assert {view.state for view in stage_views("pending", {})} == {"queued"}


def test_failed_stage_views_do_not_guess_failure_location() -> None:
    views = stage_views("failed", {"discovered": 3, "processed": 3, "failed": 1})

    assert views[0].state == "complete"
    assert {view.state for view in views[1:8]} == {"closed"}
    assert views[-1].state == "complete"


def test_error_renderer_uses_stderr_panel() -> None:
    renderer, output, errors = make_renderer()

    renderer.render(
        AppResponse(False, {"error_type": "RuntimeConnectionError"}, "Temporal is down"),
        command="doctor",
    )

    assert output.getvalue() == ""
    rendered = errors.getvalue()
    assert "HarborRAG error" in rendered
    assert "Temporal is down" in rendered
    assert "RuntimeConnectionError" in rendered
