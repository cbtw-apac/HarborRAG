from __future__ import annotations

import pytest

from harborrag_core.schemas.storage import HealthStatus, StorageFamily

from .fakes import CONTEXT_V2, FakeDatabaseClientV2, make_cache_backend


def test_cache_backend_client_property_delegates_to_database_raw() -> None:
    fake_client = FakeDatabaseClientV2(connected=True)
    backend = make_cache_backend(fake_client)

    assert backend.client is fake_client


@pytest.mark.asyncio
async def test_cache_backend_connect_and_close_delegate() -> None:
    fake_client = FakeDatabaseClientV2(connected=False)
    backend = make_cache_backend(fake_client)

    await backend.connect()
    assert fake_client.connect_calls == 1

    await backend.close()
    assert fake_client.close_calls == 1


@pytest.mark.asyncio
async def test_cache_backend_health_unknown_when_not_connected() -> None:
    backend = make_cache_backend(FakeDatabaseClientV2(connected=False))

    health = await backend.health()

    assert health.status == HealthStatus.UNKNOWN
    assert health.instance_name == "primary"


@pytest.mark.asyncio
async def test_cache_backend_health_healthy_when_connected() -> None:
    backend = make_cache_backend(FakeDatabaseClientV2(connected=True))

    health = await backend.health()

    assert health.status == HealthStatus.HEALTHY


def test_cache_backend_key_helpers_build_prefixed_and_escaped_keys() -> None:
    backend = make_cache_backend(FakeDatabaseClientV2())

    assert backend.value_key(CONTEXT_V2, "k:1") == "harborrag:v1:tenant-a:cache:k%3A1"
    assert backend.tags_key(CONTEXT_V2, "k:1") == "harborrag:v1:tenant-a:cache_tags:k%3A1"
    assert backend.tags_key_for_value_key(backend.value_key(CONTEXT_V2, "k")) == (
        backend.tags_key(CONTEXT_V2, "k")
    )
    assert backend.tag_key(CONTEXT_V2, "t:1") == "harborrag:v1:tenant-a:tag:t%3A1"
    assert backend.lock_key(CONTEXT_V2, "l:1") == "harborrag:v1:tenant-a:lock:l%3A1"
    assert backend.fencing_key(CONTEXT_V2, "l:1") == "harborrag:v1:tenant-a:fence:l%3A1"


def test_cache_backend_error_context_includes_operation_and_resource() -> None:
    backend = make_cache_backend(FakeDatabaseClientV2())

    error_context = backend.error_context("get", "resource-1")

    assert error_context.operation == "get"
    assert error_context.resource_name == "resource-1"
    assert error_context.instance_name == "primary"
    assert error_context.family == StorageFamily.CACHE
