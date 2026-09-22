"""Fixtures for the Qdrant memory index, reusing the vector fakes next door."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from harborrag_adapters.repositories.vector.memory_index import QdrantMemoryIndex
from harborrag_adapters.repositories.vector.qdrant import (
    collections as collections_module,
)
from harborrag_adapters.repositories.vector.qdrant import query as query_module
from harborrag_adapters.repositories.vector.qdrant import (
    repository as repository_module,
)
from harborrag_adapters.repositories.vector.qdrant.repository import QdrantVectorRepository
from harborrag_core.ports.memory import (
    Memory,
    MemoryOwner,
    MemoryScope,
    MemoryType,
)

from ..test_vector_qdrant.fakes import (
    ExtendedModels,
    ExtendedQdrantClient,
    ExtendedRawQdrant,
    make_config,
)

DIMENSIONS = 3
EMBEDDING = [0.1, 0.2, 0.3]


@pytest.fixture(autouse=True)
def fake_qdrant_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository_module, "qm", ExtendedModels)
    monkeypatch.setattr(collections_module, "qm", ExtendedModels)
    monkeypatch.setattr(query_module, "qm", ExtendedModels)


@pytest.fixture
def raw() -> ExtendedRawQdrant:
    client = ExtendedRawQdrant()
    # A fresh tenant: ensure_index takes the create-collection branch.
    client.exists = False
    return client


@pytest.fixture
def repository(raw: ExtendedRawQdrant) -> QdrantVectorRepository:
    return QdrantVectorRepository(
        make_config(),
        client=ExtendedQdrantClient(raw),  # type: ignore[arg-type]
    )


class RecordingEmbedder:
    """Dense embedder stub that records every text it was asked to embed."""

    def __init__(self, vector: Sequence[float] | None = None) -> None:
        self.texts: list[str] = []
        self._vector = list(vector if vector is not None else EMBEDDING)

    async def __call__(self, text: str) -> Sequence[float]:
        self.texts.append(text)
        return list(self._vector)


@pytest.fixture
def embedder() -> RecordingEmbedder:
    return RecordingEmbedder()


@pytest.fixture
def index(
    repository: QdrantVectorRepository,
    embedder: RecordingEmbedder,
) -> QdrantMemoryIndex:
    return QdrantMemoryIndex(repository, dimensions=DIMENSIONS, embedder=embedder)


def owner(**overrides: Any) -> MemoryOwner:
    fields: dict[str, Any] = {
        "tenant_id": "tenant-a",
        "project_id": "proj-1",
        "user_id": "user-1",
        "principal_id": "principal-1",
        "session_id": "session-1",
        "run_id": "run-1",
    }
    fields.update(overrides)
    return MemoryOwner(**fields)


def memory(**overrides: Any) -> Memory:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    fields: dict[str, Any] = {
        "memory_id": "mem-1",
        "scope": MemoryScope.USER,
        "memory_type": MemoryType.FACT,
        "owner": owner(),
        "content": "prefers concise answers",
        "importance": 0.75,
        "created_at": now,
        "updated_at": now,
        "content_hash": "sha256:abc",
    }
    fields.update(overrides)
    return Memory(**fields)


def hit(memory_id: str, score: float) -> SimpleNamespace:
    """One provider point: the payload carries the canonical memory identity."""

    return SimpleNamespace(
        id=f"point-{memory_id}",
        score=score,
        payload={"memory_id": memory_id},
        vector=None,
    )


def must_conditions(call: dict[str, Any]) -> dict[str, Any]:
    """Flatten one recorded query's ``must`` list into field -> matched value."""

    flattened: dict[str, Any] = {}
    for condition in call["query_filter"].must or []:
        is_null = getattr(condition, "is_null", None)
        if is_null is not None:
            flattened[f"{is_null.key}:is_null"] = True
            continue
        match = condition.match
        value = getattr(match, "value", None)
        flattened[condition.key] = value if value is not None else match.any
    return flattened
