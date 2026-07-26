from __future__ import annotations

import json
import threading

import pytest
from app_test_fixtures import MockAppService

from harborrag_app.cli import main as cli
from harborrag_app.cli import runner as cli_runner
from harborrag_app.workflow_control.client import AppService
from harborrag_runtime.composition import CompositionRoot
from harborrag_runtime.temporal.identity import RuntimeWorkflowRef
from harborrag_runtime.temporal.schemas import (
    IngestionSummary,
    RunProgress,
    RunStatus,
    WorkflowStatusView,
)


class FakeTemporalClient:
    def __init__(self) -> None:
        self.started = []
        self.signals: list[tuple[str, str, object]] = []

    async def start_ingestion(self, request):
        self.started.append(request)
        return RuntimeWorkflowRef(request.run_id, "workflow-1", "execution-1")

    async def health(self):
        return True

    async def result(self, run_id):
        return IngestionSummary(run_id, "manifest-1", RunStatus.COMPLETED, RunProgress())

    async def get_status(self, run_id):
        return WorkflowStatusView(
            run_id,
            RunStatus.RUNNING,
            RunProgress(discovered=2),
            0,
            False,
            False,
        )

    async def execution_status(self, run_id):
        return "running"

    async def get_progress(self, run_id):
        return RunProgress(discovered=2)

    async def get_failed_artifacts(self, run_id):
        return ()

    async def get_quarantined_artifacts(self, run_id):
        return ()

    async def get_pending_resolutions(self, run_id):
        return ()

    async def pause(self, run_id):
        self.signals.append((run_id, "pause", None))

    async def resume(self, run_id):
        self.signals.append((run_id, "resume", None))

    async def cancel(self, run_id, *, graceful=True):
        self.signals.append((run_id, "cancel", graceful))

    async def retry_failed(self, run_id, artifact_ids):
        self.signals.append((run_id, "retry", artifact_ids))


@pytest.mark.asyncio
async def test_app_service_submits_queries_and_controls_temporal() -> None:
    temporal = FakeTemporalClient()

    async def connect(config):
        return temporal

    service = AppService(
        CompositionRoot(control_db={"ping": "ok"}),
        client_factory=connect,  # type: ignore[arg-type]
    )
    started = await service.start_ingestion(
        tenant_id="tenant-1",
        connector_name="local-docs",
        run_id="run-1",
        manifest_id="manifest-1",
        generation_id="generation-1",
        max_artifacts=3,
    )
    assert started.ok
    assert temporal.started[0].connector_name == "local-docs"
    assert temporal.started[0].options.max_artifacts == 3
    health = await service.runtime_health()
    assert health.ok and health.data["runtime"]["provider"] == "temporal"
    status = await service.ingestion_status("run-1")
    assert status.ok and status.data["progress"]["discovered"] == 2
    retried = await service.control_ingestion(
        "run-1",
        "retry",
        artifact_ids=("document-1",),
    )
    assert retried.ok
    assert temporal.signals[-1] == ("run-1", "retry", ("document-1",))


def test_ingest_cli_has_stable_json_envelope(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    exit_code = cli.main(
        [
            "ingest",
            "start",
            "--tenant",
            "tenant-1",
            "--connector",
            "local-docs",
            "--run-id",
            "run-1",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["error"] is None
    assert payload["data"]["run"]["run_id"] == "run-1"


def test_ingest_cli_constructs_service_outside_event_loop(monkeypatch, capsys) -> None:
    caller_thread = threading.get_ident()
    factory_threads: list[int] = []

    def build_service() -> MockAppService:
        factory_threads.append(threading.get_ident())
        return MockAppService()

    monkeypatch.setattr(cli_runner, "runtime_app_service", build_service)

    exit_code = cli.main(
        [
            "ingest",
            "status",
            "run-1",
            "--json",
        ]
    )

    capsys.readouterr()
    assert exit_code == 0
    assert factory_threads
    assert factory_threads[0] != caller_thread


def test_ingest_cli_uses_rich_human_summary(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    exit_code = cli.main(
        [
            "ingest",
            "start",
            "--tenant",
            "tenant-1",
            "--connector",
            "local-docs",
            "--run-id",
            "run-1",
        ]
    )

    rendered = capsys.readouterr().out
    assert exit_code == 0
    assert "Ingestion started" in rendered
    assert "run-1" in rendered
    assert "local-docs" in rendered
