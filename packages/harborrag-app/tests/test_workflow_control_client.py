"""Envelope behaviour of the Temporal-backed application service.

`AppService` is the app package's only translation layer between CLI/API calls
and the runtime client. Its contract is that every failure becomes a stable
`AppResponse` envelope rather than a raised exception, and that the runtime
client is created once and reused. Neither was covered.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from workflow_control_fixtures import (
    FakeComposition,
    FakeRuntimeClient,
    FakeTaskRegistry,
    build_service,
    source_input,
)
from workflow_control_fixtures import (
    service_resources as service_resources,
)

from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_app.workflow_control.composition.factories import AppServiceFactories
from harborrag_app.workflow_control.composition.service import AppService
from harborrag_app.workflow_control.ingestion.client import PublicIngestionClientMixin
from harborrag_app.workflow_control.ingestion.durable import DurableIngestionOperations
from harborrag_core.contracts.errors import HarborNotFoundError
from harborrag_core.contracts.events import HarborEvent
from harborrag_core.ingestion import IngestionTask, IngestionTaskState, TaskRegistration
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.errors import (
    RuntimeConnectionError,
    WorkflowNotFoundError,
    WorkflowSubmissionError,
)
from harborrag_runtime.execution.gateway import IngestionGatewayDescription
from harborrag_runtime.execution.task_registry import IngestionTaskRegistry
from harborrag_runtime.ingestion_contracts import PreparedSourceSubmission

# --------------------------------------------------------------------------
# Composition-level health
# --------------------------------------------------------------------------


def test_health_reports_ready_from_composition_diagnostics() -> None:
    service, _, _ = build_service()

    response = service.health()

    assert response.ok is True
    assert response.error is None
    assert response.data["diagnostics"] == {"runtime": {"ready": True}}


@pytest.mark.parametrize(
    "diagnostics",
    [{"runtime": {"ready": False}}, {"runtime": "not-a-mapping"}, {}],
)
def test_health_reports_not_ready_for_any_unusable_diagnostics(diagnostics: object) -> None:
    service, _, _ = build_service(diagnostics=diagnostics)

    response = service.health()

    assert response.ok is False
    assert response.error == "runtime not ready"


def test_ingest_once_directs_the_caller_to_the_workflow_command() -> None:
    service, _, _ = build_service()

    response = service.ingest_once()

    assert response.ok is False
    assert "ingest start" in str(response.error)


# --------------------------------------------------------------------------
# Runtime calls
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_health_reports_a_ready_target() -> None:
    service, _, _ = build_service()

    response = await service.runtime_health()

    assert response.ok is True
    assert response.data["runtime"]["provider"] == "temporal"
    assert "target" in response.data["runtime"]


@pytest.mark.asyncio
async def test_application_can_use_an_alternative_ingestion_gateway() -> None:
    client = FakeRuntimeClient()
    configured = []

    async def connect(settings: RuntimeSettings):
        configured.append(settings)
        return client

    async def registry(_settings):
        return FakeTaskRegistry()

    settings = RuntimeSettings()
    service = AppService(
        FakeComposition({"runtime": {"ready": True}}),
        settings=settings,
        factories=AppServiceFactories(
            client=connect,
            ingestion_description=lambda _settings: IngestionGatewayDescription(
                provider="alternative", health_timeout_seconds=1, details={"queue": "ingestion"}
            ),
            source_input_builder=source_input,
            task_registry=registry,
        ),
    )

    response = await service.start_ingestion(tenant_id="tenant-1", connector_name="docs")
    health = await service.runtime_health()

    assert response.ok
    assert configured == [settings]
    submitted = client.calls[0][1][0]
    assert type(submitted) is PreparedSourceSubmission
    assert not hasattr(submitted, "workflow_options")
    assert health.data["runtime"] == {
        "provider": "alternative",
        "ready": True,
        "queue": "ingestion",
    }
    await service.aclose()


@pytest.mark.asyncio
async def test_runtime_health_enforces_the_configured_timeout(tmp_path) -> None:
    config_path = tmp_path / "temporal.yaml"
    config_path.write_text("version: 1\nhealth:\n  timeout_seconds: 0.05\n", encoding="utf-8")

    class SlowRuntimeClient(FakeRuntimeClient):
        async def health(self) -> bool:
            await asyncio.sleep(0.2)
            return await super().health()

    client = SlowRuntimeClient()

    async def client_factory(_config):
        return client

    service = AppService(
        FakeComposition({"runtime": {"ready": True}}),
        settings=RuntimeSettings(temporal_config_path=config_path),
        factories=AppServiceFactories(client=client_factory, source_input_builder=source_input),
    )

    response = await service.runtime_health()

    assert response.ok is False
    assert response.data["error_type"] == "TimeoutError"


@pytest.mark.asyncio
async def test_start_ingestion_generates_identifiers_and_returns_the_reference() -> None:
    service, client, _ = build_service()

    response = await service.start_ingestion(tenant_id="tenant-1", connector_name="local_file")

    assert response.ok is True
    assert response.data["run"]["tenant_id"] == "tenant-1"
    assert response.data["run"]["run_id"].startswith("ingestion-")
    assert response.data["workflow"]["workflow_id"] == "wf-1"
    assert "result" not in response.data
    assert [name for name, _ in client.calls] == ["start_ingestion"]


@pytest.mark.asyncio
async def test_start_ingestion_honours_explicit_identifiers_and_waiting() -> None:
    service, client, _ = build_service()

    response = await service.start_ingestion(
        tenant_id="tenant-1",
        connector_name="local_file",
        run_id="run-explicit",
        max_artifacts=3,
        wait=True,
    )

    assert response.data["run"]["run_id"] == "run-explicit"
    request = client.calls[0][1][0]
    assert isinstance(request, PreparedSourceSubmission)
    assert request.query.limit == 3
    assert response.data["result"]["processed"] == 2
    assert [name for name, _ in client.calls] == ["start_ingestion", "result"]


@pytest.mark.asyncio
async def test_start_ingestion_honours_explicit_batching_overrides() -> None:
    service, client, _ = build_service()

    response = await service.start_ingestion(
        tenant_id="tenant-1",
        connector_name="local_file",
        batch_size=5,
        document_concurrency=5,
    )

    assert response.ok is True
    request = client.calls[0][1][0]
    assert isinstance(request, PreparedSourceSubmission)
    assert request.batch_size == 5
    assert request.document_concurrency == 5


@pytest.mark.asyncio
async def test_start_ingestion_defaults_batching_when_unset() -> None:
    service, client, _ = build_service()

    await service.start_ingestion(tenant_id="tenant-1", connector_name="local_file")

    request = client.calls[0][1][0]
    assert isinstance(request, PreparedSourceSubmission)
    assert request.batch_size == 200
    assert request.document_concurrency == 8


@pytest.mark.asyncio
async def test_start_persists_pending_task_before_temporal_submission() -> None:
    events: list[str] = []
    client = FakeRuntimeClient()

    async def client_factory(_config) -> FakeRuntimeClient:
        original = client.start_ingestion

        async def start(request):
            events.append("temporal")
            return await original(request)

        client.start_ingestion = start
        return client

    class OrderedRegistry(FakeTaskRegistry):
        async def register(self, source: PreparedSourceSubmission) -> TaskRegistration:
            events.append("postgres")
            return await super().register(source)

    async def registry_factory(_settings: RuntimeSettings) -> OrderedRegistry:
        return OrderedRegistry()

    service = AppService(
        FakeComposition({"runtime": {"ready": True}}),
        factories=AppServiceFactories(
            client=client_factory,
            source_input_builder=source_input,
            task_registry=registry_factory,
        ),
    )

    response = await service.start_ingestion(
        tenant_id="tenant-1",
        connector_name="local",
    )

    assert response.ok is True
    assert events == ["postgres", "temporal"]


@pytest.mark.asyncio
async def test_ingestion_status_aggregates_every_query() -> None:
    service, client, _ = build_service()

    response = await service.ingestion_status("run-1")

    assert response.ok is True
    assert response.data["status"]["status"] == "RUNNING"
    assert response.data["execution_status"] == "running"
    assert len(client.calls) == 3


@pytest.mark.asyncio
async def test_ingestion_result_returns_the_run_summary() -> None:
    service, _, _ = build_service()

    response = await service.ingestion_result("run-1")

    assert response.ok is True
    assert response.data["result"]["discovered"] == 2


# --------------------------------------------------------------------------
# Control actions
# --------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["pause", "resume", "cancel"])
@pytest.mark.asyncio
async def test_control_actions_are_forwarded(action: str) -> None:
    service, client, _ = build_service()

    response = await service.control_ingestion("run-1", action)

    assert response.ok is True
    assert response.data["action"] == action
    assert client.calls[0][0] == action


@pytest.mark.parametrize("action", ["pause", "resume", "cancel"])
@pytest.mark.asyncio
async def test_public_control_actions_forward_the_actor(action: str) -> None:
    # The HTTP routes pass actor=principal.subject; the facade must accept it.
    calls: list[tuple[str, str, str | None]] = []

    class RecordingIngestions:
        def __getattr__(self, name: str):
            async def control(task_id: str, *, actor: str | None = None) -> dict[str, object]:
                calls.append((name, task_id, actor))
                return {"task_id": task_id}

            return control

    facade = PublicIngestionClientMixin()
    facade._public_ingestions = RecordingIngestions()  # type: ignore[assignment]

    await getattr(facade, action)("run-1", actor="alice")

    assert calls == [(action, "run-1", "alice")]


@pytest.mark.asyncio
async def test_an_unsupported_action_is_refused() -> None:
    service, _, _ = build_service()

    response = await service.control_ingestion("run-1", "detonate")

    assert response.ok is False
    assert "unsupported ingestion action" in str(response.error)


# --------------------------------------------------------------------------
# Failure envelope and client reuse
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_failures_become_a_stable_envelope() -> None:
    service, _, _ = build_service(FakeRuntimeClient(failure=RuntimeError("temporal is down")))

    for response in (
        await service.runtime_health(),
        await service.ingestion_status("run-1"),
        await service.ingestion_result("run-1"),
        await service.control_ingestion("run-1", "pause"),
        await service.start_ingestion(tenant_id="t", connector_name="c"),
    ):
        assert response.ok is False
        assert response.data["error_type"] == "RuntimeError"
        assert response.error == "RuntimeError"


@pytest.mark.asyncio
async def test_an_exception_without_a_message_still_names_its_type() -> None:
    service, _, _ = build_service(FakeRuntimeClient(failure=RuntimeError()))

    response = await service.ingestion_result("run-1")

    assert response.error == "RuntimeError"


@pytest.mark.asyncio
async def test_the_runtime_client_is_created_once_and_reused() -> None:
    service, _, factory_calls = build_service()

    await service.ingestion_result("run-1")
    await service.ingestion_result("run-2")
    await asyncio.gather(
        service.ingestion_result("run-3"),
        service.ingestion_result("run-4"),
    )

    assert len(factory_calls) == 1


@pytest.mark.asyncio
async def test_aclose_closes_the_composition_root() -> None:
    service, _, _ = build_service()

    await service.aclose()

    assert service._composition.closed is True


# --------------------------------------------------------------------------
# Run lifecycle: submission outcome, run-id reuse, status fallback
# --------------------------------------------------------------------------


def _durable(registry: object, client: object) -> DurableIngestionOperations:
    async def runtime_client() -> Any:
        if isinstance(client, Exception):
            raise client
        return client

    async def task_registry() -> Any:
        return registry

    return DurableIngestionOperations(
        RuntimeSettings(),
        runtime_client=runtime_client,
        task_registry=task_registry,
        source_input_builder=source_input,
    )


@pytest.mark.asyncio
async def test_unreachable_temporal_fails_the_run_and_reports_its_id(
    service_resources: tuple[object, IngestionControlPlaneDatabase, object],
) -> None:
    _, control, _ = service_resources
    operations = _durable(
        IngestionTaskRegistry(control),
        RuntimeConnectionError("down"),
    )

    response = await operations.start_ingestion(
        tenant_id="tenant-1", connector_name="local", run_id="run-unreachable"
    )

    assert response.ok is False
    assert response.data["run"]["run_id"] == "run-unreachable"
    assert response.data["run"]["status"] == "FAILED"
    assert response.data["error_type"] == "RuntimeConnectionError"
    assert response.error == "down"
    task = await control.tasks.get("run-unreachable")
    assert task is not None
    assert task.status is IngestionTaskState.FAILED
    assert task.summary == {"failed_stage": "submission", "error_code": "submission_failed"}
    pending = await control.tasks.pending_submissions()
    assert "run-unreachable" not in {item.task_id for item in pending}


@pytest.mark.asyncio
async def test_ambiguous_submission_failure_stays_pending_with_guidance() -> None:
    registry = FakeTaskRegistry()
    client = FakeRuntimeClient(
        failure=WorkflowSubmissionError("could not submit ingestion run 'run-ambiguous'")
    )
    operations = _durable(registry, client)

    response = await operations.start_ingestion(
        tenant_id="tenant-1", connector_name="local", run_id="run-ambiguous"
    )

    assert response.ok is False
    assert response.data["run"]["run_id"] == "run-ambiguous"
    assert response.data["run"]["status"] == "PENDING"
    assert response.data["error_type"] == "WorkflowSubmissionError"
    assert "may still start" in str(response.error)
    assert "harborrag ingest status run-ambiguous" in str(response.error)
    assert registry.tasks["run-ambiguous"].status is IngestionTaskState.PENDING
    assert registry.transitions == []
    assert registry.summaries == []


@pytest.mark.asyncio
async def test_successful_start_marks_submission() -> None:
    registry = FakeTaskRegistry()
    operations = _durable(registry, FakeRuntimeClient())

    response = await operations.start_ingestion(
        tenant_id="tenant-1", connector_name="local", run_id="run-started"
    )

    assert response.ok is True
    assert registry.summaries == [("run-started", {"submission_state": "submitted"})]


@pytest.mark.asyncio
async def test_a_failed_submission_marker_does_not_fail_a_started_run() -> None:
    class UnwritableRegistry(FakeTaskRegistry):
        async def update_summary(self, task_id: str, values: dict[str, object]) -> None:
            raise RuntimeError("control plane went away")

    operations = _durable(UnwritableRegistry(), FakeRuntimeClient())

    response = await operations.start_ingestion(
        tenant_id="tenant-1", connector_name="local", run_id="run-started"
    )

    assert response.ok is True
    assert response.data["run"]["status"] == "PENDING"


@pytest.mark.asyncio
async def test_reused_terminal_run_id_is_rejected() -> None:
    registry = FakeTaskRegistry()
    registry.tasks["run-done"] = IngestionTask(
        task_id="run-done",
        source_scope_id="scope-1",
        status=IngestionTaskState.COMPLETED,
        request={"tenant_id": "tenant-1"},
    )
    client = FakeRuntimeClient()
    operations = _durable(registry, client)

    response = await operations.start_ingestion(
        tenant_id="tenant-1", connector_name="local", run_id="run-done"
    )

    assert response.ok is False
    assert response.data["error_type"] == "WorkflowRunAlreadyStartedError"
    assert "already used" in str(response.error)
    assert "run-done" in str(response.error)
    assert client.calls == []
    assert registry.tasks["run-done"].status is IngestionTaskState.COMPLETED


@pytest.mark.asyncio
async def test_control_action_succeeds_without_a_local_task_row() -> None:
    class RowlessRegistry(FakeTaskRegistry):
        async def append_task_event(self, task_id: str, event: HarborEvent) -> HarborEvent:
            raise HarborNotFoundError(f"ingestion task does not exist: {task_id}")

    client = FakeRuntimeClient()
    operations = _durable(RowlessRegistry(), client)

    response = await operations.control_ingestion("run-elsewhere", "cancel")

    assert response.ok is True
    assert response.data == {"run_id": "run-elsewhere", "action": "cancel"}
    assert client.calls == [("cancel", ("run-elsewhere",))]


@pytest.mark.asyncio
async def test_status_falls_back_to_control_plane_for_unknown_workflow() -> None:
    registry = FakeTaskRegistry()
    registry.tasks["run-lost"] = IngestionTask(
        task_id="run-lost",
        source_scope_id="scope-1",
        status=IngestionTaskState.FAILED,
        request={"tenant_id": "tenant-1", "connector_type": "local", "connection_id": "local"},
        summary={"failed_stage": "submission", "error_code": "submission_failed"},
    )
    registry.counts["run-lost"] = {"published": 1, "failed": 1}
    client = FakeRuntimeClient(failure=WorkflowNotFoundError("ingestion run 'run-lost' not found"))
    operations = _durable(registry, client)

    response = await operations.ingestion_status("run-lost")

    assert response.ok is True
    assert response.data["execution_status"] == "not_found"
    assert response.data["source"] == "control_plane"
    assert response.data["status"]["run_id"] == "run-lost"
    assert response.data["status"]["status"] == "FAILED"
    assert response.data["progress"]["succeeded"] == 1
    assert response.data["progress"]["failed"] == 1
    assert response.data["task"]["task_id"] == "run-lost"


@pytest.mark.asyncio
async def test_status_of_a_run_unknown_everywhere_stays_not_found() -> None:
    client = FakeRuntimeClient(failure=WorkflowNotFoundError("ingestion run 'ghost' not found"))
    operations = _durable(FakeTaskRegistry(), client)

    response = await operations.ingestion_status("ghost")

    assert response.ok is False
    assert response.data["error_type"] == "WorkflowNotFoundError"


@pytest.mark.asyncio
async def test_app_service_fails_the_run_when_the_temporal_connect_fails() -> None:
    # The production chain: AppService -> AppResources.runtime_client -> client factory.
    registry = FakeTaskRegistry()

    async def unreachable(_settings: RuntimeSettings) -> FakeRuntimeClient:
        raise RuntimeConnectionError("Could not connect to Temporal gRPC frontend 'h:7233'")

    async def registry_factory(_settings: RuntimeSettings) -> FakeTaskRegistry:
        return registry

    service = AppService(
        FakeComposition({"runtime": {"ready": True}}),
        factories=AppServiceFactories(
            client=unreachable,
            source_input_builder=source_input,
            task_registry=registry_factory,
        ),
    )

    response = await service.start_ingestion(
        tenant_id="tenant-1", connector_name="local", run_id="run-offline"
    )

    assert response.ok is False
    assert response.data["run"] == {"run_id": "run-offline", "status": "FAILED"}
    assert registry.transitions == [
        (
            "run-offline",
            IngestionTaskState.FAILED,
            {"failed_stage": "submission", "error_code": "submission_failed"},
        )
    ]
