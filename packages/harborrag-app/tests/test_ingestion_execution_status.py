"""A crashed workflow must not keep reporting itself as running.

``get_status`` is answered from state the workflow tracks about itself, so a run
whose workflow raised never records its own failure and replies "running"
forever. These tests pin the three places that now reconcile that against
Temporal's server-side execution status.
"""

from __future__ import annotations

from io import StringIO

import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient
from rich.console import Console

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_app.cli.rendering import CliRenderer
from harborrag_app.cli.stages import headline_status
from harborrag_app.workflow_control import AppResponse
from harborrag_runtime.errors import (
    WorkflowNotFoundError,
    WorkflowNotRunningError,
    WorkflowRunAlreadyStartedError,
)


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


class _MissingRunAppService(MockAppService):
    """Answers status as the real service does when Temporal has no such run."""

    async def ingestion_status(self, run_id: str) -> AppResponse:
        return AppResponse(
            False,
            {"error_type": WorkflowNotFoundError.__name__},
            f"Could not describe ingestion run {run_id!r}: run not found",
        )


def test_unknown_run_is_not_found_rather_than_an_upstream_fault(monkeypatch) -> None:
    """A bad run ID must not send operators hunting a broken Temporal cluster."""

    monkeypatch.setattr(
        api_app,
        "select_app_service",
        lambda: (_MissingRunAppService(), "test"),
    )
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        response = client.get("/api/v1/ingestions/no-such-run")

    assert response.status_code == 404
    body = response.json()["error"]
    assert body["code"] == "ingestion_run_not_found"
    assert "no-such-run" in body["message"]


class _ConflictAppService(MockAppService):
    """Reports the two run-state conflicts the runtime distinguishes."""

    async def control_ingestion(
        self,
        run_id: str,
        action: str,
        *,
        artifact_ids: tuple[str, ...] = (),
        graceful: bool = True,
    ) -> AppResponse:
        return AppResponse(
            False,
            {"error_type": WorkflowNotRunningError.__name__},
            f"Could not signal {action} for {run_id!r}: the run already finished",
        )

    async def start_ingestion(self, **kwargs: object) -> AppResponse:
        return AppResponse(
            False,
            {"error_type": WorkflowRunAlreadyStartedError.__name__},
            "Could not start ingestion run 'run-1': the run ID is already in use",
        )


@pytest.fixture
def conflict_client(monkeypatch) -> TestClient:
    monkeypatch.setattr(
        api_app,
        "select_app_service",
        lambda: (_ConflictAppService(), "test"),
    )
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        yield client


def test_controlling_a_finished_run_is_a_conflict(conflict_client: TestClient) -> None:
    """The run exists, so neither 404 nor 502 describes what went wrong."""

    response = conflict_client.post(
        "/api/v1/ingestions/run-1/actions",
        json={"action": "pause"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ingestion_run_not_running"


def test_reusing_a_run_id_is_a_conflict(conflict_client: TestClient) -> None:
    response = conflict_client.post(
        "/api/v1/ingestions",
        json={"tenant_id": "tenant-1", "connector_name": "local", "run_id": "run-1"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ingestion_run_already_started"
