"""Envelope behaviour of the Temporal-backed application service.

`AppService` is the app package's only translation layer between CLI/API calls
and the runtime client. Its contract is that every failure becomes a stable
`AppResponse` envelope rather than a raised exception, and that the runtime
client is created once and reused. Neither was covered.
"""

from __future__ import annotations

import asyncio

import pytest

from harborrag_runtime.application.composition.factories import AppServiceFactories
from harborrag_runtime.application.composition.service import AppService
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.execution.gateway import IngestionGatewayDescription

from ..application_fixtures import (
    FakeComposition,
    FakeRuntimeClient,
    FakeTaskRegistry,
    build_service,
    source_input,
)
from ..application_fixtures import (
    service_resources as service_resources,
)

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

    health = await service.runtime_health()

    assert configured == [settings]
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
async def test_runtime_failures_become_a_stable_envelope() -> None:
    service, _, _ = build_service(FakeRuntimeClient(failure=RuntimeError("temporal is down")))

    response = await service.runtime_health()

    assert response.ok is False
    assert response.data["error_type"] == "RuntimeError"
    assert response.error == "RuntimeError"


@pytest.mark.asyncio
async def test_an_exception_without_a_message_still_names_its_type() -> None:
    service, _, _ = build_service(FakeRuntimeClient(failure=RuntimeError()))

    response = await service.runtime_health()

    assert response.error == "RuntimeError"


@pytest.mark.asyncio
async def test_the_runtime_client_is_created_once_and_reused() -> None:
    service, _, factory_calls = build_service()

    await service.runtime_health()
    await service.runtime_health()
    await asyncio.gather(service.runtime_health(), service.runtime_health())

    assert len(factory_calls) == 1


@pytest.mark.asyncio
async def test_aclose_closes_the_composition_root() -> None:
    service, _, _ = build_service()

    await service.aclose()

    assert service._composition.closed is True
