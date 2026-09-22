"""The memory vector index is built from retrieval's connected client, or skipped."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory import build_memory_index
from harborrag_runtime.memory.context_service import RuntimeMemoryContextService

pytestmark = pytest.mark.unit


class FakeResources:
    """The two members ``MemoryIndexResources`` requires, and nothing else."""

    def __init__(self, *, dimensions: int = 8) -> None:
        self._dimensions = dimensions
        self.vector_repository = object()

    @property
    def embedding_dimensions(self) -> int:
        return self._dimensions


def _provider(resources: Any) -> Any:
    async def provide() -> Any:
        return resources

    return provide


class FakeMemories:
    """Minimal memory repository: the builder only wires recall when one exists."""

    async def save(self, memory: Any) -> None:
        del memory

    async def get(self, caller: Any, memory_id: str) -> None:
        del caller, memory_id
        return None

    async def search(self, query: Any) -> tuple[Any, ...]:
        del query
        return ()

    async def delete(self, caller: Any, memory_id: str) -> None:
        del caller, memory_id


def _builder_index(context_builder: Any) -> Any:
    """The index the builder handed to its recall step."""

    recall = context_builder._recall
    assert recall is not None, "a builder with a memory repository must wire recall"
    return recall._index


def _failing_provider() -> Any:
    async def provide() -> Any:
        raise RuntimeError("qdrant is unreachable")

    return provide


@pytest.mark.asyncio
async def test_index_is_built_over_the_retrieval_clients_vector_repository() -> None:
    resources = FakeResources(dimensions=16)

    index = await build_memory_index(RuntimeSettings(), _provider(resources))

    assert index is not None
    # The memory index must share retrieval's connected client, not open its own.
    assert getattr(index, "_repository", None) is resources.vector_repository
    assert getattr(index, "_dimensions", None) == 16


@pytest.mark.asyncio
async def test_disabled_memory_builds_no_index() -> None:
    settings = RuntimeSettings(memory_enabled=False)

    assert await build_memory_index(settings, _provider(FakeResources())) is None


@pytest.mark.asyncio
async def test_recall_switched_off_builds_no_index(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nothing would read it, so do not pay to create the collection."""

    settings = RuntimeSettings(memory_recall_top_k=0)

    with caplog.at_level(logging.INFO, logger="harborrag.runtime.memory.index"):
        assert await build_memory_index(settings, _provider(FakeResources())) is None

    assert any("recall is disabled" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_unreachable_retrieval_degrades_to_repository_search(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="harborrag.runtime.memory.index"):
        index = await build_memory_index(RuntimeSettings(), _failing_provider())

    assert index is None
    assert any("falls back to repository search" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_non_positive_vector_width_is_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="harborrag.runtime.memory.index"):
        index = await build_memory_index(RuntimeSettings(), _provider(FakeResources(dimensions=0)))

    assert index is None
    assert any("non-positive embedding width" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_the_context_service_caches_one_index() -> None:
    calls: list[int] = []

    async def builder(settings: RuntimeSettings, provider: Any) -> object:
        del settings, provider
        calls.append(1)
        return object()

    service = RuntimeMemoryContextService(
        RuntimeSettings(),
        index_provider=_provider(FakeResources()),
        index_builder=builder,  # type: ignore[arg-type]
    )

    first = await service.index()
    second = await service.index()

    assert first is second
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_no_index_provider_means_no_index() -> None:
    """An embedded caller with no vector store still gets a working builder."""

    service = RuntimeMemoryContextService(RuntimeSettings())

    assert await service.index() is None


@pytest.mark.asyncio
async def test_the_builder_falls_back_to_the_runtime_index() -> None:
    sentinel = object()

    async def builder(settings: RuntimeSettings, provider: Any) -> object:
        del settings, provider
        return sentinel

    service = RuntimeMemoryContextService(
        RuntimeSettings(),
        model_builder=_none_builder,
        embedder_builder=_none_builder,
        index_provider=_provider(FakeResources()),
        index_builder=builder,  # type: ignore[arg-type]
    )

    context_builder = await service.builder(
        messages=object(),  # type: ignore[arg-type]
        memories=FakeMemories(),  # type: ignore[arg-type]
    )

    assert _builder_index(context_builder) is sentinel


@pytest.mark.asyncio
async def test_a_caller_supplied_index_wins_over_the_runtime_one() -> None:
    caller = object()

    async def builder(settings: RuntimeSettings, provider: Any) -> object:
        del settings, provider
        raise AssertionError("the runtime index must not be built when one is supplied")

    service = RuntimeMemoryContextService(
        RuntimeSettings(),
        model_builder=_none_builder,
        embedder_builder=_none_builder,
        index_provider=_provider(FakeResources()),
        index_builder=builder,  # type: ignore[arg-type]
    )

    context_builder = await service.builder(
        messages=object(),  # type: ignore[arg-type]
        memories=FakeMemories(),  # type: ignore[arg-type]
        index=caller,  # type: ignore[arg-type]
    )

    assert _builder_index(context_builder) is caller


@pytest.mark.asyncio
async def test_closing_drops_the_cached_index_without_closing_retrievals_client() -> None:
    """Retrieval opened the vector client, so retrieval closes it."""

    resources = FakeResources()
    service = RuntimeMemoryContextService(
        RuntimeSettings(),
        model_builder=_none_builder,
        embedder_builder=_none_builder,
        index_provider=_provider(resources),
    )
    first = await service.index()
    assert first is not None

    await service.aclose()

    assert getattr(first, "_repository", None) is resources.vector_repository
    assert await service.index() is not first


async def _none_builder(settings: RuntimeSettings) -> None:
    del settings
    return None
