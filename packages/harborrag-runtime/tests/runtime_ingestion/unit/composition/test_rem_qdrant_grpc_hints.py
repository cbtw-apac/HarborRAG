from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from harborrag_runtime.composition.resources import resource_hints
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.errors import RuntimeConnectionError
from harborrag_runtime.ingestion.composition import IngestionRuntime

pytestmark = pytest.mark.unit

_GRPC_VARIABLES = ("HARBORRAG_QDRANT_GRPC_PORT", "HARBORRAG_QDRANT_PREFER_GRPC=false")


def _runtime_whose_vector_store_is_unreachable() -> IngestionRuntime:
    runtime = object.__new__(IngestionRuntime)
    runtime.connector_pool = SimpleNamespace(aclose=AsyncMock())  # type: ignore[assignment]
    runtime.connection_stores = None
    runtime.telemetry = SimpleNamespace(start=AsyncMock(), close=AsyncMock())  # type: ignore[assignment]
    runtime.connector_rate_limiter = SimpleNamespace(close=Mock())  # type: ignore[assignment]
    runtime.embed_client = SimpleNamespace(aclose=AsyncMock())  # type: ignore[assignment]
    runtime._started = False
    runtime.control = SimpleNamespace(connect=AsyncMock(), close=AsyncMock())
    runtime.object_store = SimpleNamespace(
        connect=AsyncMock(), close=AsyncMock(), ensure_buckets=AsyncMock()
    )
    runtime.vector_repository = SimpleNamespace(
        connect=AsyncMock(side_effect=OSError("unreachable")), close=AsyncMock()
    )
    runtime.graph_repository = SimpleNamespace(connect=AsyncMock(), close=AsyncMock())
    return runtime


async def _fallback_vector_hint() -> str:
    with pytest.raises(RuntimeConnectionError) as failure:
        await _runtime_whose_vector_store_is_unreachable().start()
    return str(failure.value).split("; ", 1)[1]


@pytest.mark.asyncio
async def test_the_fallback_vector_hint_names_both_grpc_settings() -> None:
    hint = await _fallback_vector_hint()

    for variable in _GRPC_VARIABLES:
        assert variable in hint


@pytest.mark.asyncio
async def test_the_grpc_resource_hint_matches_the_fallback_hint() -> None:
    configured = resource_hints(RuntimeSettings(qdrant_prefer_grpc=True))["vector"]

    assert configured == await _fallback_vector_hint()
    for variable in _GRPC_VARIABLES:
        assert variable in configured
