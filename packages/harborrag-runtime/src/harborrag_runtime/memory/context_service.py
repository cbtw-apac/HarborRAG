"""Runtime ownership of the memory policy, chat model, and embedder.

The policy, the chat model, and the embedder are process-wide -- they come
from settings and a catalog -- so they are built once and cached. The message
store, the memory repository, and the vector index are not: the application
owns those resources and hands them in per call. A builder is therefore
assembled fresh for every request so one caller's store can never leak into
another caller's context.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from harborrag_memory import MemoryContextBuilder, MemoryPolicy

from .embedder import build_memory_embedder, close_memory_embedder
from .entities import GraphLookupProvider, build_memory_entity_resolver
from .index import MemoryIndexProvider, build_memory_index
from .model import build_memory_chat_model, close_memory_chat_model
from .policy import memory_policy_from_settings

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

    from harborrag_core.ports.conversation import ConversationMessageStore
    from harborrag_core.ports.memory import (
        MemoryEmbedder,
        MemoryEntityResolver,
        MemoryIndex,
        MemoryRepository,
    )
    from harborrag_runtime.config.settings import RuntimeSettings

type MemoryModelBuilder = Callable[["RuntimeSettings"], Awaitable["BaseChatModel | None"]]
type MemoryEmbedderBuilder = Callable[["RuntimeSettings"], Awaitable["MemoryEmbedder | None"]]
type MemoryEntityResolverBuilder = Callable[
    ["RuntimeSettings", GraphLookupProvider],
    Awaitable["MemoryEntityResolver | None"],
]
type MemoryIndexBuilder = Callable[
    ["RuntimeSettings", MemoryIndexProvider],
    Awaitable["MemoryIndex | None"],
]


class RuntimeMemoryContextService:
    """Assemble memory-context builders from cached settings-derived parts."""

    def __init__(  # noqa: PLR0913 - one keyword-only builder per cached part
        self,
        settings: RuntimeSettings,
        *,
        model_builder: MemoryModelBuilder | None = None,
        embedder_builder: MemoryEmbedderBuilder | None = None,
        retrieval_provider: GraphLookupProvider | None = None,
        entity_resolver_builder: MemoryEntityResolverBuilder | None = None,
        index_provider: MemoryIndexProvider | None = None,
        index_builder: MemoryIndexBuilder | None = None,
    ) -> None:
        self._settings = settings
        self._model_builder = model_builder or build_memory_chat_model
        self._embedder_builder = embedder_builder or build_memory_embedder
        self._retrieval_provider = retrieval_provider
        self._entity_resolver_builder = entity_resolver_builder or build_memory_entity_resolver
        self._resolver: MemoryEntityResolver | None = None
        self._resolver_built = False
        self._resolver_lock = asyncio.Lock()
        self._policy: MemoryPolicy | None = None
        self._model: BaseChatModel | None = None
        self._model_built = False
        self._model_lock = asyncio.Lock()
        self._embedder: MemoryEmbedder | None = None
        self._embedder_built = False
        self._embedder_lock = asyncio.Lock()
        self._index_provider = index_provider
        self._index_builder = index_builder or build_memory_index
        self._index: MemoryIndex | None = None
        self._index_built = False
        self._index_lock = asyncio.Lock()

    @property
    def settings(self) -> RuntimeSettings:
        """The settings this service was composed from."""

        return self._settings

    @property
    def policy(self) -> MemoryPolicy:
        """The configured policy, resolved from settings once."""

        if self._policy is None:
            self._policy = memory_policy_from_settings(self._settings)
        return self._policy

    async def model(self) -> BaseChatModel | None:
        """The memory chat model, built at most once; ``None`` when unavailable.

        A disabled policy never rewrites, summarizes, or extracts, so no
        catalog is read and no client is opened for it.
        """

        if not self.policy.enabled:
            return None
        if self._model_built:
            return self._model
        async with self._model_lock:
            if not self._model_built:
                self._model = await self._model_builder(self._settings)
                self._model_built = True
        return self._model

    async def embedder(self) -> MemoryEmbedder | None:
        """The memory embedder, built at most once; ``None`` when unavailable.

        A disabled policy neither recalls nor extracts, so no embed catalog is
        read and no client is opened for it.
        """

        if not self.policy.enabled:
            return None
        if self._embedder_built:
            return self._embedder
        async with self._embedder_lock:
            if not self._embedder_built:
                self._embedder = await self._embedder_builder(self._settings)
                self._embedder_built = True
        return self._embedder

    async def entity_resolver(self) -> MemoryEntityResolver | None:
        """The knowledge-graph entity resolver, built at most once; ``None`` when off.

        ``None`` -- never an exception -- when the policy is disabled, when no
        retrieval provider was wired (an embedded caller with no graph), or
        when the builder decided the graph cannot be read. Memory then stores
        the surface forms it was given and recall ranks without anchors.
        """

        if not self.policy.enabled or self._retrieval_provider is None:
            return None
        if self._resolver_built:
            return self._resolver
        async with self._resolver_lock:
            if not self._resolver_built:
                self._resolver = await self._entity_resolver_builder(
                    self._settings,
                    self._retrieval_provider,
                )
                self._resolver_built = True
        return self._resolver

    async def index(self) -> MemoryIndex | None:
        """The memory vector index, built at most once; ``None`` when unavailable.

        ``None`` -- never an exception -- when the policy is disabled, when no
        index provider was wired (an embedded caller with no vector store), or
        when the builder could not reach retrieval. Recall then falls back to
        the repository's substring search.
        """

        if not self.policy.enabled or self._index_provider is None:
            return None
        if self._index_built:
            return self._index
        async with self._index_lock:
            if not self._index_built:
                self._index = await self._index_builder(
                    self._settings,
                    self._index_provider,
                )
                self._index_built = True
        return self._index

    async def builder(
        self,
        *,
        messages: ConversationMessageStore,
        memories: MemoryRepository | None = None,
        index: MemoryIndex | None = None,
    ) -> MemoryContextBuilder:
        """Build one request-scoped context builder over the caller's stores.

        A caller that supplies no index gets the runtime's own, so semantic
        recall works without every application wiring a vector store itself.
        """

        return MemoryContextBuilder(
            policy=self.policy,
            messages=messages,
            memories=memories,
            model=await self.model(),
            index=index or await self.index(),
            embedder=await self.embedder(),
        )

    async def aclose(self) -> None:
        """Close the cached clients under the locks that build them.

        Mirrors ``RuntimeChatService.aclose``: swapping and closing inside the
        same lock keeps a concurrent ``model()``/``embedder()`` call from
        building a second client that nothing ever closes.
        """

        async with self._model_lock:
            model, self._model = self._model, None
            self._model_built = False
            await close_memory_chat_model(model)
        async with self._embedder_lock:
            embedder, self._embedder = self._embedder, None
            self._embedder_built = False
            await close_memory_embedder(embedder)
        async with self._resolver_lock:
            # The resolver owns no client of its own: it borrows retrieval
            # through the provider, and whoever opened retrieval closes it.
            # Dropping the cache is all there is to dispose.
            self._resolver = None
            self._resolver_built = False
        async with self._index_lock:
            # Likewise the index: it shares retrieval's vector client, so
            # retrieval's owner closes it.
            self._index = None
            self._index_built = False


__all__ = [
    "MemoryEmbedderBuilder",
    "MemoryEntityResolverBuilder",
    "MemoryIndexBuilder",
    "MemoryModelBuilder",
    "RuntimeMemoryContextService",
]
