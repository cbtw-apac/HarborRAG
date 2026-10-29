"""Ingestion runtime composition behavior."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from harborrag_runtime.errors import RuntimeConnectionError
from harborrag_runtime.ingestion.composition import IngestionRuntime


@pytest.mark.asyncio
async def test_runtime_logs_resource_lifecycle(caplog: pytest.LogCaptureFixture) -> None:
    pool = SimpleNamespace(aclose=AsyncMock())
    stores = SimpleNamespace(aclose=AsyncMock())
    control = SimpleNamespace(connect=AsyncMock(), close=AsyncMock())
    object_store = SimpleNamespace(
        connect=AsyncMock(),
        close=AsyncMock(),
        ensure_buckets=AsyncMock(),
    )
    vector_repository = SimpleNamespace(connect=AsyncMock(), close=AsyncMock())
    graph_repository = SimpleNamespace(connect=AsyncMock(), close=AsyncMock())
    telemetry = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
    rate_limiter = SimpleNamespace(close=Mock())
    embed_client = SimpleNamespace(aclose=AsyncMock())
    runtime = object.__new__(IngestionRuntime)
    runtime.connector_pool = pool  # type: ignore[assignment]
    runtime.connection_stores = stores  # type: ignore[assignment]
    runtime.control = control
    runtime.object_store = object_store
    runtime.vector_repository = vector_repository
    runtime.graph_repository = graph_repository
    runtime.telemetry = telemetry
    runtime.connector_rate_limiter = rate_limiter
    runtime.embed_client = embed_client
    runtime._started = False

    with caplog.at_level(logging.INFO, logger="harborrag.runtime.ingestion.composition"):
        await runtime.start()
        await runtime.close()

    pool.aclose.assert_awaited_once_with()
    stores.aclose.assert_awaited_once_with()
    assert "Ingestion runtime started resources=5" in caplog.text
    assert "Ingestion runtime closed" in caplog.text


@pytest.mark.asyncio
async def test_runtime_close_attempts_every_resource_and_remains_retryable() -> None:
    pool = SimpleNamespace(aclose=AsyncMock())
    control = SimpleNamespace(close=AsyncMock(side_effect=[RuntimeError("busy"), None]))
    object_store = SimpleNamespace(close=AsyncMock())
    vector_repository = SimpleNamespace(close=AsyncMock())
    graph_repository = SimpleNamespace(close=AsyncMock())
    telemetry = SimpleNamespace(close=AsyncMock())
    rate_limiter = SimpleNamespace(close=Mock())
    embed_client = SimpleNamespace(aclose=AsyncMock())
    runtime = object.__new__(IngestionRuntime)
    runtime.connector_pool = pool  # type: ignore[assignment]
    runtime.connection_stores = None
    runtime.control = control
    runtime.object_store = object_store
    runtime.vector_repository = vector_repository
    runtime.graph_repository = graph_repository
    runtime.telemetry = telemetry
    runtime.connector_rate_limiter = rate_limiter
    runtime.embed_client = embed_client
    runtime._started = True

    with pytest.raises(BaseExceptionGroup):
        await runtime.close()

    assert runtime._started is True
    assert pool.aclose.await_count == 1
    assert graph_repository.close.await_count == 1

    await runtime.close()

    assert runtime._started is False
    assert control.close.await_count == 2


def _startable_runtime(**overrides: object) -> IngestionRuntime:
    resources = {
        "control": SimpleNamespace(connect=AsyncMock(), close=AsyncMock()),
        "object_store": SimpleNamespace(
            connect=AsyncMock(), close=AsyncMock(), ensure_buckets=AsyncMock()
        ),
        "vector_repository": SimpleNamespace(connect=AsyncMock(), close=AsyncMock()),
        "graph_repository": SimpleNamespace(connect=AsyncMock(), close=AsyncMock()),
    }
    resources.update(overrides)
    runtime = object.__new__(IngestionRuntime)
    runtime.connector_pool = SimpleNamespace(aclose=AsyncMock())  # type: ignore[assignment]
    runtime.connection_stores = None
    runtime.telemetry = SimpleNamespace(start=AsyncMock(), close=AsyncMock())  # type: ignore[assignment]
    runtime.connector_rate_limiter = SimpleNamespace(close=Mock())  # type: ignore[assignment]
    runtime.embed_client = SimpleNamespace(aclose=AsyncMock())  # type: ignore[assignment]
    runtime._started = False
    for name, resource in resources.items():
        setattr(runtime, name, resource)
    return runtime


@pytest.mark.asyncio
async def test_start_wraps_unreachable_vector_store() -> None:
    original = RuntimeError("grpc u:hunter2")
    vector = SimpleNamespace(connect=AsyncMock(side_effect=original), close=AsyncMock())
    runtime = _startable_runtime(vector_repository=vector)
    runtime.resource_targets = {"vector": "Qdrant gRPC localhost:6334"}

    with pytest.raises(RuntimeConnectionError, match="Qdrant gRPC localhost:6334") as failure:
        await runtime.start()

    assert "hunter2" not in str(failure.value)
    assert "HARBORRAG_QDRANT_PREFER_GRPC=false" in str(failure.value)
    assert failure.value.__cause__ is original
    runtime.control.close.assert_awaited_once_with()
    runtime.telemetry.close.assert_awaited_once_with()
    assert runtime._started is False


@pytest.mark.asyncio
async def test_start_wraps_bucket_provisioning_failures() -> None:
    object_store = SimpleNamespace(
        connect=AsyncMock(),
        close=AsyncMock(),
        ensure_buckets=AsyncMock(side_effect=RuntimeError("AccessKey=hunter2")),
    )
    runtime = _startable_runtime(object_store=object_store)
    runtime.resource_targets = {"object_store": "http://localhost:9000"}

    with pytest.raises(
        RuntimeConnectionError, match="Could not connect to or initialise http://localhost:9000"
    ) as failure:
        await runtime.start()

    assert "hunter2" not in str(failure.value)
    assert "HARBORRAG_OBJECT_STORE_*" in str(failure.value)
    object_store.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_start_uses_the_configured_hint_over_the_fixed_text() -> None:
    vector = SimpleNamespace(connect=AsyncMock(side_effect=OSError("x")), close=AsyncMock())
    runtime = _startable_runtime(vector_repository=vector)
    runtime.resource_targets = {"vector": "Qdrant http://localhost:6333"}
    runtime.resource_hints = {"vector": "start Qdrant and check HARBORRAG_QDRANT_URL"}

    with pytest.raises(RuntimeConnectionError) as failure:
        await runtime.start()

    assert str(failure.value).endswith("start Qdrant and check HARBORRAG_QDRANT_URL")
    assert "PREFER_GRPC" not in str(failure.value)


@pytest.mark.asyncio
async def test_start_names_the_resource_without_targets() -> None:
    graph = SimpleNamespace(connect=AsyncMock(side_effect=OSError("x")), close=AsyncMock())
    runtime = _startable_runtime(graph_repository=graph)

    with pytest.raises(RuntimeConnectionError, match="start FalkorDB"):
        await runtime.start()


@pytest.mark.asyncio
async def test_start_lets_cancellation_through() -> None:
    vector = SimpleNamespace(
        connect=AsyncMock(side_effect=asyncio.CancelledError()), close=AsyncMock()
    )
    runtime = _startable_runtime(vector_repository=vector)

    with pytest.raises(asyncio.CancelledError):
        await runtime.start()
    runtime.control.close.assert_awaited_once_with()
