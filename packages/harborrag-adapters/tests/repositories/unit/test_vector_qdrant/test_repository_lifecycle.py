from __future__ import annotations

import pytest

from harborrag_adapters.repositories.vector.qdrant import (
    collections as collections_module,
)
from harborrag_adapters.repositories.vector.qdrant import (
    repository as repository_module,
)
from harborrag_adapters.repositories.vector.qdrant.repository import QdrantVectorRepository
from harborrag_core.schemas.storage import HealthStatus

from .fakes import (
    ExtendedQdrantClient,
    ExtendedRawQdrant,
    FakeQdrantClient,
    FakeRawQdrant,
    make_config,
)


def test_repository_requires_qm_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository_module, "qm", None)
    monkeypatch.setattr(collections_module, "qm", None)
    with pytest.raises(ImportError):
        QdrantVectorRepository(make_config())


def test_capabilities_reports_dense_only_baseline() -> None:
    raw = FakeRawQdrant()
    repository = QdrantVectorRepository(make_config(), client=FakeQdrantClient(raw))  # type: ignore[arg-type]

    capabilities = repository.capabilities

    assert capabilities.dense_vectors is True
    assert capabilities.hybrid_search is False
    assert capabilities.sparse_vectors is False


@pytest.mark.asyncio
async def test_connect_and_close_delegate_to_database_client() -> None:
    raw = ExtendedRawQdrant()
    client = ExtendedQdrantClient(raw)
    repository = QdrantVectorRepository(make_config(), client=client)  # type: ignore[arg-type]

    await repository.connect()
    assert client.connect_calls == 1

    await repository.close()
    assert client.close_calls == 1


@pytest.mark.asyncio
async def test_health_reports_unknown_when_not_connected() -> None:
    raw = ExtendedRawQdrant()
    client = ExtendedQdrantClient(raw, is_connected=False)
    repository = QdrantVectorRepository(make_config(), client=client)  # type: ignore[arg-type]

    health = await repository.health()

    assert health.status == HealthStatus.UNKNOWN
    assert health.details["deployment"] == client.deployment


@pytest.mark.asyncio
async def test_health_reports_healthy_when_ping_succeeds() -> None:
    raw = ExtendedRawQdrant()
    client = ExtendedQdrantClient(raw, is_connected=True)
    repository = QdrantVectorRepository(make_config(), client=client)  # type: ignore[arg-type]

    health = await repository.health()

    assert health.status == HealthStatus.HEALTHY


@pytest.mark.asyncio
async def test_health_reports_unhealthy_when_ping_raises() -> None:
    raw = ExtendedRawQdrant()
    client = ExtendedQdrantClient(raw, is_connected=True, ping_error=RuntimeError("down"))
    repository = QdrantVectorRepository(make_config(), client=client)  # type: ignore[arg-type]

    health = await repository.health()

    assert health.status == HealthStatus.UNHEALTHY
    assert health.details["error_type"] == "RuntimeError"
