from __future__ import annotations

import pytest

from harborrag_core.schemas.storage import HealthStatus, StorageFamily

from .fakes import CONTEXT, FakeDatabaseClient, make_backend


def test_state_backend_client_property_delegates_to_database_raw() -> None:
    fake_client = FakeDatabaseClient(connected=True)
    backend = make_backend(fake_client)

    assert backend.client is fake_client


@pytest.mark.asyncio
async def test_state_backend_connect_and_close_delegate() -> None:
    fake_client = FakeDatabaseClient(connected=False)
    backend = make_backend(fake_client)

    await backend.connect()
    assert fake_client.connect_calls == 1

    await backend.close()
    assert fake_client.close_calls == 1


@pytest.mark.asyncio
async def test_state_backend_health_unknown_when_not_connected() -> None:
    backend = make_backend(FakeDatabaseClient(connected=False))

    health = await backend.health()

    assert health.status == HealthStatus.UNKNOWN
    assert health.backend == "redis"
    assert health.instance_name == "primary"


@pytest.mark.asyncio
async def test_state_backend_health_healthy_when_connected_and_ping_ok() -> None:
    backend = make_backend(FakeDatabaseClient(connected=True))

    health = await backend.health()

    assert health.status == HealthStatus.HEALTHY


def test_state_backend_key_joins_prefix_tenant_and_escaped_parts() -> None:
    backend = make_backend(FakeDatabaseClient())

    key = backend.key(CONTEXT, "workflow", "wf:1")

    assert key == "harborrag:v1:state:tenant-a:workflow:wf%3A1"


def test_state_backend_error_context_includes_tenant_and_resource() -> None:
    backend = make_backend(FakeDatabaseClient())

    error_context = backend.error_context("state_get", CONTEXT, "wf-1")

    assert error_context.operation == "state_get"
    assert error_context.tenant_id == "tenant-a"
    assert error_context.resource_name == "wf-1"
    assert error_context.instance_name == "primary"
    assert error_context.family == StorageFamily.STATE
