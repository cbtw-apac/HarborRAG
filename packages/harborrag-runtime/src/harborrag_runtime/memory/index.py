"""Conversation-memory vector index built over the connected retrieval client.

Memory embeddings live in their own logical collection (``memories``), never
the document/evidence one, because retention and erasure differ. A user can
erase their own memories without touching indexed documents. The Qdrant
*client*, though, is shared with retrieval rather than opened a second time,
so a process holds one connection pool regardless of how many logical indexes
it reads.

Without this index the memory layer still works. Recall falls back to the
repository's own substring search and extraction dedupes on content hashes
alone. So every failure path here returns ``None`` after a warning instead of
raising, because a missing index degrades recall and must not break chat.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol

from harborrag_core.observability.log_events import log_event

if TYPE_CHECKING:
    from harborrag_core.ports.memory import MemoryEmbedder, MemoryIndex
    from harborrag_core.ports.storage import VectorRepositoryPort

    from ..config.settings import RuntimeSettings

logger = logging.getLogger("harborrag.runtime.memory.index")

_INDEX_UNAVAILABLE_EVENT = "memory.index_unavailable"
_FALLBACK_HINT = "Recall falls back to repository search."


class MemoryIndexResources(Protocol):
    """The slice of runtime retrieval the memory index needs.

    Narrower than ``RuntimeRetrievalService`` on purpose. Building an index
    needs a connected vector client and the deployment's vector width, and
    nothing else, not the graph and not the document search path.
    """

    @property
    def vector_repository(self) -> VectorRepositoryPort:
        """The connected vector client shared with retrieval."""
        ...

    @property
    def embedding_dimensions(self) -> int:
        """The deployment's embedding vector width."""
        ...


type MemoryIndexProvider = Callable[[], Awaitable[MemoryIndexResources]]
"""Deferred access to retrieval, so no client is opened until first use."""


async def build_memory_index(
    settings: RuntimeSettings,
    provider: MemoryIndexProvider,
    *,
    embedder: MemoryEmbedder | None = None,
) -> MemoryIndex | None:
    """Build the memory vector index, or return ``None`` when it cannot be built.

    Returns ``None``, logged and never raised, when memory is disabled, when
    recall is switched off (``recall_top_k`` of zero, so nothing would read
    it), when retrieval cannot be reached, or when the vector width is
    unusable. Each failure degrades recall to substring search.

    Args:
        settings: Runtime settings holding the memory switches.
        provider: Opens or returns the shared retrieval resources.
        embedder: Memory-profile embedder the index embeds with, if any.

    Returns:
        The memory index, or ``None`` when it cannot be built.
    """
    if not settings.memory_enabled:
        return None
    if settings.memory_recall_top_k < 1:
        log_event(logger, "memory.index_skipped", reason="recall_disabled")
        return None
    try:
        resources = await provider()
        dimensions = resources.embedding_dimensions
        repository = resources.vector_repository
    except Exception as error:  # noqa: BLE001
        log_event(
            logger,
            _INDEX_UNAVAILABLE_EVENT,
            level=logging.WARNING,
            hint=_FALLBACK_HINT,
            reason="retrieval_unreachable",
            error=error,
        )
        return None
    if dimensions < 1:
        log_event(
            logger,
            _INDEX_UNAVAILABLE_EVENT,
            level=logging.WARNING,
            hint=_FALLBACK_HINT,
            reason="non_positive_dimensions",
            dimension_count=dimensions,
        )
        return None
    try:
        from harborrag_adapters.repositories.vector import QdrantMemoryIndex

        return QdrantMemoryIndex(repository, dimensions=dimensions, embedder=embedder)
    except Exception as error:  # noqa: BLE001
        log_event(
            logger,
            _INDEX_UNAVAILABLE_EVENT,
            level=logging.WARNING,
            hint=_FALLBACK_HINT,
            reason="index_build_failed",
            error=error,
        )
        return None
