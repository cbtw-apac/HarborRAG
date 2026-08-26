"""Envelope behaviour of the Temporal-backed application service.

`AppService` is the app package's only translation layer between CLI/API calls
and the runtime client. Its contract is that every failure becomes a stable
`AppResponse` envelope rather than a raised exception, and that the runtime
client is created once and reused. Neither was covered.
"""

from __future__ import annotations

import asyncio

import pytest
from workflow_control_fixtures import (
    FakeComposition,
    FakeRuntimeClient,
    FakeTaskRegistry,
    build_service,
    source_input,
)

from harborrag_app.workflow_control.composition.factories import AppServiceFactories
from harborrag_app.workflow_control.composition.service import AppService
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.temporal.schemas import SourceIngestionInput

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
    assert isinstance(request, SourceIngestionInput)
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
    assert isinstance(request, SourceIngestionInput)
    assert request.batch_size == 5
    assert request.document_concurrency == 5


@pytest.mark.asyncio
async def test_start_ingestion_defaults_batching_when_unset() -> None:
    service, client, _ = build_service()

    await service.start_ingestion(tenant_id="tenant-1", connector_name="local_file")

    request = client.calls[0][1][0]
    assert isinstance(request, SourceIngestionInput)
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
        async def register(self, source: SourceIngestionInput) -> None:
            events.append("postgres")
            await super().register(source)

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
