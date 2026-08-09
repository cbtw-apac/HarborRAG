"""Ingestion runtime composition behavior."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from harborrag_runtime.config.errors import ConnectorConfigurationError
from harborrag_runtime.ingestion.composition import IngestionRuntime


def test_runtime_rejects_submission_worker_connector_drift() -> None:
    connector = object()
    runtime = object.__new__(IngestionRuntime)
    runtime.connectors = {"docs": connector}  # type: ignore[assignment]
    runtime.connector_fingerprints = {"docs": "connector-current"}

    assert (
        runtime.connector(
            "docs",
            configuration_fingerprint="connector-current",
        )
        is connector
    )
    with pytest.raises(ConnectorConfigurationError, match="differs"):
        runtime.connector(
            "docs",
            configuration_fingerprint="connector-stale",
        )


@pytest.mark.asyncio
async def test_runtime_logs_resource_lifecycle(caplog: pytest.LogCaptureFixture) -> None:
    connector = SimpleNamespace(connect=Mock(), close=Mock())
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
    runtime.connectors = {"docs": connector}
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

    assert "Ingestion runtime started resources=5 connectors=1" in caplog.text
    assert "Ingestion runtime closed connectors=1" in caplog.text


@pytest.mark.asyncio
async def test_runtime_close_attempts_every_resource_and_remains_retryable() -> None:
    connector = SimpleNamespace(close=Mock())
    control = SimpleNamespace(close=AsyncMock(side_effect=[RuntimeError("busy"), None]))
    object_store = SimpleNamespace(close=AsyncMock())
    vector_repository = SimpleNamespace(close=AsyncMock())
    graph_repository = SimpleNamespace(close=AsyncMock())
    telemetry = SimpleNamespace(close=AsyncMock())
    rate_limiter = SimpleNamespace(close=Mock())
    embed_client = SimpleNamespace(aclose=AsyncMock())
    runtime = object.__new__(IngestionRuntime)
    runtime.connectors = {"docs": connector}
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
    assert connector.close.call_count == 1
    assert graph_repository.close.await_count == 1

    await runtime.close()

    assert runtime._started is False
    assert control.close.await_count == 2
