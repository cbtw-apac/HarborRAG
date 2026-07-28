"""Coverage for the CLI render paths not exercised by the status view.

``CliRenderer.render`` dispatches on command/action, and several branches --
doctor, start, the pause/resume/cancel/retry controls, the generic tree
fallback, and the error path -- had no test. These assert the operator-visible
text for each, since that output is the CLI's entire contract.
"""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from harborrag_app.cli.rendering import CliRenderer
from harborrag_app.workflow_control import AppResponse


def make_renderer() -> tuple[CliRenderer, StringIO, StringIO]:
    output = StringIO()
    errors = StringIO()
    renderer = CliRenderer(
        console=Console(file=output, force_terminal=False, width=100),
        error_console=Console(file=errors, force_terminal=False, width=100),
    )
    return renderer, output, errors


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------


def test_doctor_reports_a_ready_runtime() -> None:
    renderer, output, _ = make_renderer()
    response = AppResponse(
        True,
        {"runtime": {"ready": True, "provider": "temporal", "target": "localhost:7233"}},
    )

    renderer.render(response, command="doctor")

    text = output.getvalue()
    assert "Runtime ready" in text
    assert "temporal" in text
    assert "localhost:7233" in text


def test_doctor_reports_an_unavailable_runtime() -> None:
    renderer, output, _ = make_renderer()

    renderer.render(AppResponse(True, {"runtime": {"ready": False}}), command="doctor")

    text = output.getvalue()
    assert "Runtime unavailable" in text
    # Absent fields fall back to placeholders rather than raising.
    assert "unknown" in text


# --------------------------------------------------------------------------
# start
# --------------------------------------------------------------------------


def test_start_lists_run_identity_and_a_pending_stage_table() -> None:
    renderer, output, _ = make_renderer()
    response = AppResponse(
        True,
        {
            "run": {
                "run_id": "run-1",
                "tenant_id": "tenant-1",
                "connector_name": "local_file",
                "manifest_id": "manifest-1",
                "generation_id": "gen-1",
            },
            "workflow": {"workflow_id": "wf-1"},
        },
    )

    renderer.render(response, command="ingest", action="start")

    text = output.getvalue()
    assert "Ingestion started" in text
    assert "run-1" in text
    assert "local_file" in text
    assert "wf-1" in text


def test_start_shows_a_summary_when_the_run_already_returned_a_result() -> None:
    renderer, output, _ = make_renderer()
    response = AppResponse(
        True,
        {
            "run": {"run_id": "run-1"},
            "workflow": {"workflow_id": "wf-1"},
            "result": {"discovered": 4, "processed": 4, "succeeded": 4},
        },
    )

    renderer.render(response, command="ingest", action="start")

    assert "run-1" in output.getvalue()


# --------------------------------------------------------------------------
# control actions
# --------------------------------------------------------------------------


def test_control_action_is_acknowledged() -> None:
    renderer, output, _ = make_renderer()
    response = AppResponse(True, {"action": "pause", "run_id": "run-1"})

    renderer.render(response, command="ingest", action="pause")

    text = output.getvalue()
    assert "Pause accepted" in text
    assert "run-1" in text


def test_retry_reports_a_pluralised_artifact_count() -> None:
    renderer, output, _ = make_renderer()
    response = AppResponse(
        True,
        {"action": "retry", "run_id": "run-1", "artifact_ids": ["a-1", "a-2"]},
    )

    renderer.render(response, command="ingest", action="retry")

    assert "2 artifacts" in output.getvalue()


def test_retry_reports_a_single_artifact_without_a_plural() -> None:
    renderer, output, _ = make_renderer()
    response = AppResponse(True, {"action": "retry", "run_id": "run-1", "artifact_ids": ["a-1"]})

    renderer.render(response, command="ingest", action="retry")

    text = output.getvalue()
    assert "1 artifact)" in text


# --------------------------------------------------------------------------
# errors and the generic fallback
# --------------------------------------------------------------------------


def test_a_failed_response_renders_on_the_error_console() -> None:
    renderer, output, errors = make_renderer()
    response = AppResponse(False, {"error_type": "connector_unavailable"}, error="Boom happened")

    renderer.render(response, command="ingest", action="start")

    assert errors.getvalue() != ""
    assert "Boom happened" in errors.getvalue()
    assert "connector_unavailable" in errors.getvalue()
    assert output.getvalue() == ""


def test_an_unknown_command_falls_back_to_a_tree() -> None:
    renderer, output, _ = make_renderer()
    response = AppResponse(
        True,
        {
            "plain_value": "hello",
            "nested_mapping": {"inner_key": 1},
            "sequence_value": ["first", "second"],
        },
    )

    renderer.render(response, command="whatever")

    text = output.getvalue()
    assert "Result" in text
    # Keys are humanised, and nested mappings/sequences become branches.
    assert "Plain Value" in text
    assert "Nested Mapping" in text
    assert "Inner Key" in text
    assert "first" in text
    assert "second" in text


# --------------------------------------------------------------------------
# operation spinner
# --------------------------------------------------------------------------


def test_operation_context_manager_can_be_disabled() -> None:
    renderer, output, _ = make_renderer()

    with renderer.operation("working", enabled=False):
        pass

    assert output.getvalue() == ""


def test_operation_context_manager_yields_when_enabled() -> None:
    renderer, _, _ = make_renderer()
    entered = False

    with renderer.operation("working"):
        entered = True

    assert entered is True


# --------------------------------------------------------------------------
# status extras
# --------------------------------------------------------------------------


def test_status_falls_back_to_nested_progress_and_lists_pending_resolutions() -> None:
    renderer, output, _ = make_renderer()
    response = AppResponse(
        True,
        {
            "status": {
                "run_id": "run-1",
                "status": "paused",
                "paused": True,
                "cancel_requested": True,
                "progress": {"discovered": 4, "processed": 2},
            },
            "quarantined_artifacts": ["doc-9"],
            "pending_resolutions": [
                {"artifact_id": "doc-9", "reason": "schema", "resume_stage": "index"},
            ],
        },
    )

    renderer.render(response, command="status")

    text = output.getvalue()
    assert "Ingestion paused" in text
    assert "Quarantined artifacts" in text
    assert "Pending resolutions" in text
    assert "doc-9" in text
    assert "index" in text


def test_status_without_discovery_shows_a_waiting_bar() -> None:
    renderer, output, _ = make_renderer()
    response = AppResponse(
        True,
        {
            "status": {"run_id": "run-1", "status": "pending"},
            "progress": {},
        },
    )

    renderer.render(response, command="status")

    assert "Waiting for discovery" in output.getvalue()
