"""The memory vector index is built from retrieval's connected client, or skipped."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import pytest

from harborrag_core.ports.memory import Memory, MemoryOwner, MemoryQuery
from harborrag_memory import MemoryContextBuilder
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory import build_memory_index
from harborrag_runtime.memory.context_service import RuntimeMemoryContextService

pytestmark = pytest.mark.unit

type ResourcesProvider = Callable[[], Awaitable[FakeResources]]


def _records(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.__dict__.get("event") == event]


class FakeResources:
    """The two members ``MemoryIndexResources`` requires, and nothing else."""

    def __init__(self, *, dimensions: int = 8) -> None:
        self._dimensions = dimensions
        self.vector_repository = object()

    @property
    def embedding_dimensions(self) -> int:
        return self._dimensions


def _provider(resources: FakeResources) -> ResourcesProvider:
    async def provide() -> FakeResources:
        return resources

    return provide


class FakeMemories:
    """Minimal memory repository: the builder only wires recall when one exists."""

    async def save(self, memory: Memory) -> None:
        del memory

    async def get(self, caller: MemoryOwner, memory_id: str) -> None:
        del caller, memory_id
        return None

    async def search(self, query: MemoryQuery) -> tuple[Memory, ...]:
        del query
        return ()

    async def delete(self, caller: MemoryOwner, memory_id: str) -> None:
        del caller, memory_id


def _builder_index(context_builder: MemoryContextBuilder) -> object:
    """Return the index the builder handed to its recall step."""
    recall = context_builder._recall
    assert recall is not None, "a builder with a memory repository must wire recall"
    return recall._index


def _failing_provider() -> ResourcesProvider:
    async def provide() -> FakeResources:
        raise RuntimeError("qdrant is unreachable")

    return provide


@pytest.mark.asyncio
async def test_index_is_built_over_the_retrieval_clients_vector_repository() -> None:
    """The memory index shares retrieval's connected client, never opening its own."""
    resources = FakeResources(dimensions=16)

    index = await build_memory_index(RuntimeSettings(), _provider(resources))

    assert index is not None
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

    (record,) = _records(caplog, "memory.index_skipped")
    assert record.__dict__["reason"] == "recall_disabled"


@pytest.mark.asyncio
async def test_unreachable_retrieval_degrades_to_repository_search(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="harborrag.runtime.memory.index"):
        index = await build_memory_index(RuntimeSettings(), _failing_provider())

    assert index is None
    (record,) = _records(caplog, "memory.index_unavailable")
    assert record.levelno == logging.WARNING
    assert record.__dict__["reason"] == "retrieval_unreachable"
    assert record.__dict__["error_type"] == "RuntimeError"
    assert "falls back to repository search" in record.getMessage()


@pytest.mark.asyncio
async def test_a_non_positive_vector_width_is_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="harborrag.runtime.memory.index"):
        index = await build_memory_index(RuntimeSettings(), _provider(FakeResources(dimensions=0)))

    assert index is None
    (record,) = _records(caplog, "memory.index_unavailable")
    assert record.__dict__["reason"] == "non_positive_dimensions"
    assert record.__dict__["dimension_count"] == 0


@pytest.mark.asyncio
async def test_the_context_service_caches_one_index() -> None:
    calls: list[int] = []

    async def builder(settings: RuntimeSettings, provider: object) -> object:
        del settings, provider
        calls.append(1)
        return object()

    service = RuntimeMemoryContextService(
        RuntimeSettings(),
        index_provider=_provider(FakeResources()),
        index_builder=builder,  # type: ignore[arg-type]  # a sentinel stands in for the index
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

    async def builder(settings: RuntimeSettings, provider: object) -> object:
        del settings, provider
        return sentinel

    service = RuntimeMemoryContextService(
        RuntimeSettings(),
        model_builder=_none_builder,
        embedder_builder=_none_builder,
        index_provider=_provider(FakeResources()),
        index_builder=builder,  # type: ignore[arg-type]  # a sentinel stands in for the index
    )

    context_builder = await service.builder(
        messages=object(),  # type: ignore[arg-type]  # a sentinel stands in for the store
        memories=FakeMemories(),  # type: ignore[arg-type]  # a fake with the calls recall makes
    )

    assert _builder_index(context_builder) is sentinel


@pytest.mark.asyncio
async def test_a_caller_supplied_index_wins_over_the_runtime_one() -> None:
    caller = object()

    async def builder(settings: RuntimeSettings, provider: object) -> object:
        del settings, provider
        raise AssertionError("the runtime index must not be built when one is supplied")

    service = RuntimeMemoryContextService(
        RuntimeSettings(),
        model_builder=_none_builder,
        embedder_builder=_none_builder,
        index_provider=_provider(FakeResources()),
        index_builder=builder,  # type: ignore[arg-type]  # a builder that must never run
    )

    context_builder = await service.builder(
        messages=object(),  # type: ignore[arg-type]  # a sentinel stands in for the store
        memories=FakeMemories(),  # type: ignore[arg-type]  # a fake with the calls recall makes
        index=caller,  # type: ignore[arg-type]  # a sentinel stands in for the index
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
