"""Build the conversation-memory vector index over the connected retrieval client.

Memory embeddings live in their own logical collection (``memories``), never
the document/evidence one, because retention and erasure differ: a user can
erase their own memories without touching indexed documents. The Qdrant
*client*, though, is shared with retrieval rather than opened a second time,
so a process holds one connection pool regardless of how many logical indexes
it reads.

Without this index the memory layer still works: recall falls back to the
repository's own substring search and extraction dedupes on content hashes
alone. So every failure path here returns ``None`` after a warning instead of
raising -- a missing index degrades recall, it must not break chat.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from harborrag_adapters.repositories.vector.base import HarborVectorRepository
    from harborrag_core.ports.memory import MemoryEmbedder, MemoryIndex

    from ..config.settings import RuntimeSettings

logger = logging.getLogger("harborrag.runtime.memory.index")


class MemoryIndexResources(Protocol):
    """The slice of runtime retrieval the memory index needs.

    Narrower than ``RuntimeRetrievalService`` on purpose: building an index
    needs a connected vector client and the deployment's vector width, and
    nothing else -- not the graph, not the document search path.
    """

    @property
    def vector_repository(self) -> HarborVectorRepository: ...

    @property
    def embedding_dimensions(self) -> int: ...


type MemoryIndexProvider = Callable[[], Awaitable[MemoryIndexResources]]
"""Deferred access to retrieval, so no client is opened until first use."""


async def build_memory_index(
    settings: RuntimeSettings,
    provider: MemoryIndexProvider,
    *,
    embedder: MemoryEmbedder | None = None,
) -> MemoryIndex | None:
    """The memory vector index, or ``None`` when it cannot be built.

    ``None`` -- logged, never raised -- when memory is disabled, when recall
    is switched off (``recall_top_k`` of zero, so nothing would read it), when
    retrieval cannot be reached, or when the vector width is unusable.
    """

    if not settings.memory_enabled:
        return None
    if settings.memory_recall_top_k < 1:
        logger.info("Conversation-memory recall is disabled (top_k=0); skipping the memory index.")
        return None
    try:
        resources = await provider()
        dimensions = resources.embedding_dimensions
        repository = resources.vector_repository
    except Exception:  # noqa: BLE001 - degrade to substring recall
        logger.warning(
            "Could not reach retrieval to build the conversation-memory index; "
            "recall falls back to repository search.",
            exc_info=True,
        )
        return None
    if dimensions < 1:
        logger.warning(
            "Retrieval reports a non-positive embedding width (%s); "
            "skipping the conversation-memory index.",
            dimensions,
        )
        return None
    try:
        from harborrag_adapters.repositories.vector import QdrantMemoryIndex

        return QdrantMemoryIndex(repository, dimensions=dimensions, embedder=embedder)
    except Exception:  # noqa: BLE001 - degrade to substring recall
        logger.warning(
            "Could not build the conversation-memory index; recall falls back to "
            "repository search.",
            exc_info=True,
        )
        return None


__all__ = ["MemoryIndexProvider", "MemoryIndexResources", "build_memory_index"]
