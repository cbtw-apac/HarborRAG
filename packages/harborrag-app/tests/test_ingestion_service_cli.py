from __future__ import annotations

import json
import threading

import pytest
from app_test_fixtures import MockAppService

from harborrag_app.cli import main as cli
from harborrag_app.cli import runner as cli_runner
from harborrag_app.workflow_control import AppResponse
from harborrag_app.workflow_control.composition.factories import AppServiceFactories
from harborrag_app.workflow_control.composition.service import AppService
from harborrag_runtime.composition import CompositionRoot
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.temporal.identity import RuntimeWorkflowRef
from harborrag_runtime.temporal.schemas import (
    ProcessingProfileInput,
    SourceIngestionInput,
    SourceIngestionResult,
    SourceIngestionStatus,
)
from harborrag_runtime.temporal.submission import SourceSubmission


class FakeTemporalClient:
    def __init__(self) -> None:
        self.started = []
        self.signals: list[tuple[str, str, object]] = []

    async def start_ingestion(self, request):
        self.started.append(request)
        return RuntimeWorkflowRef(request.task_id, "workflow-1", "execution-1")

    async def health(self):
        return True

    async def result(self, run_id):
        return SourceIngestionResult(
            task_id=run_id,
            scan_id="scan-1",
            discovered=2,
            published=2,
            unchanged=0,
            failed=0,
            removal_candidates=(),
            unresolved_relations=0,
        )

    async def get_status(self, run_id):
        return SourceIngestionStatus(
            task_id=run_id,
            status="RUNNING",
            paused=False,
            cancel_requested=False,
        )

    async def execution_status(self, run_id):
        return "running"

    async def get_progress(self, run_id):
        return {"discovered": 2, "published": 1, "unchanged": 0, "failed": 0}

    async def pause(self, run_id):
        self.signals.append((run_id, "pause", None))

    async def resume(self, run_id):
        self.signals.append((run_id, "resume", None))

    async def cancel(self, run_id):
        self.signals.append((run_id, "cancel", None))


class FakeTaskRegistry:
    async def register(self, source: SourceIngestionInput) -> None:
        del source

    async def close(self) -> None:
        return None


def _source_input(
    _settings: RuntimeSettings,
    submission: SourceSubmission,
) -> SourceIngestionInput:
    return SourceIngestionInput(
        task_id=submission.task_id,
        tenant_id=submission.tenant_id,
        connector_name=submission.connector_name,
        connector_type="local",
        connection_id=submission.connection_id or submission.connector_name,
        source_scope_id=submission.source_scope_id or "scope-1",
        configuration_fingerprint="config-v1",
        processing=ProcessingProfileInput(
            parser_profile="parser-v1",
            normalizer_version="normalizer-v1",
            chunk_strategy="chunks-v1",
            dense_encoder_profile="dense-v1",
            sparse_encoder_profile="sparse-v1",
            graph_projection_version="graph-v1",
        ),
        query=submission.query,
    )


@pytest.mark.asyncio
async def test_app_service_submits_queries_and_controls_temporal() -> None:
    temporal = FakeTemporalClient()

    async def connect(config):
        return temporal

    async def connect_registry(_settings: RuntimeSettings) -> FakeTaskRegistry:
        return FakeTaskRegistry()

    service = AppService(
        CompositionRoot(control_db={"ping": "ok"}, mode="test"),
        factories=AppServiceFactories(
            client=connect,  # type: ignore[arg-type]
            source_input_builder=_source_input,
            task_registry=connect_registry,
        ),
    )
    started = await service.start_ingestion(
        tenant_id="tenant-1",
        connector_name="local-docs",
        run_id="run-1",
        max_artifacts=3,
    )
    assert started.ok
    assert temporal.started[0].connector_name == "local-docs"
    assert temporal.started[0].query.limit == 3
    health = await service.runtime_health()
    assert health.ok and health.data["runtime"]["provider"] == "temporal"
    status = await service.ingestion_status("run-1")
    assert status.ok and status.data["progress"]["discovered"] == 2
    paused = await service.control_ingestion("run-1", "pause")
    assert paused.ok
    assert temporal.signals[-1] == ("run-1", "pause", None)


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


def test_ingest_cli_passes_batching_overrides(monkeypatch, capsys) -> None:
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
            "--batch-size",
            "5",
            "--document-concurrency",
            "5",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["data"]["run"]["batch_size"] == 5
    assert payload["data"]["run"]["document_concurrency"] == 5


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


class DegradedControlPlaneService(MockAppService):
    """A service that booted, but whose control-plane migrations failed.

    This is the state composition deliberately leaves behind: the service exists and
    accepts calls, while the schema behind it is stale.
    """

    def health(self) -> AppResponse:
        return AppResponse(
            False,
            {
                "diagnostics": {
                    "mode": "production",
                    "runtime": {
                        "provider": "app_test_double",
                        "ready": False,
                        "control_db": {
                            "ping": "failed",
                            "error": (
                                "migrations failed: (sqlite3.OperationalError) table "
                                "projects already exists\n[SQL: CREATE TABLE projects ...]"
                            ),
                        },
                    },
                }
            },
            error="runtime not ready",
        )


def test_ingest_start_refuses_to_run_against_an_unmigrated_control_plane(
    monkeypatch,
    capsys,
) -> None:
    """Doing the work anyway defers the failure to an opaque missing-column error."""

    monkeypatch.setattr(cli_runner, "runtime_app_service", DegradedControlPlaneService)

    exit_code = cli.main(
        ["ingest", "start", "--tenant", "tenant-1", "--connector", "local-docs", "--json"]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["ok"] is False
    assert payload["data"]["error_type"] == "ControlPlaneUnavailable"
    # Neither the rendered sentence nor machine-readable data disclose SQL diagnostics.
    assert "table projects already exists" not in payload["error"]
    assert "CREATE TABLE" not in payload["error"]
    assert "detail" not in payload["data"]


def test_doctor_still_runs_when_the_control_plane_is_degraded(monkeypatch, capsys) -> None:
    """A degraded control plane is exactly when an operator reaches for diagnostics.

    Doctor still exits non-zero here -- it is reporting the degradation, which is its
    job. What matters is that it produced its own health report rather than being
    short-circuited by the readiness gate, which would have told the operator to run
    the command they were already running.
    """

    monkeypatch.setattr(cli_runner, "runtime_app_service", DegradedControlPlaneService)

    exit_code = cli.main(["doctor", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["data"].get("error_type") != "ControlPlaneUnavailable"
    assert "diagnostics" in payload["data"]


def test_an_unreportable_health_check_does_not_block_the_command(monkeypatch, capsys) -> None:
    """An inconclusive check must not become a second failure mode of its own."""

    class NoHealthService(MockAppService):
        def health(self) -> AppResponse:
            raise RuntimeError("health unavailable")

    monkeypatch.setattr(cli_runner, "runtime_app_service", NoHealthService)

    exit_code = cli.main(
        ["ingest", "start", "--tenant", "tenant-1", "--connector", "local-docs", "--json"]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["ok"] is True
