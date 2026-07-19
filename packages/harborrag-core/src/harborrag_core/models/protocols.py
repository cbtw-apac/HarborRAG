from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from .chat import (
    HarborChatMessage,
    HarborChatRequest,
    HarborChatResponse,
    HarborChatStreamChunk,
)
from .embed import HarborEmbedRequest, HarborEmbedResponse, RawEmbeddingInput
from .rerank import HarborRerankRequest, HarborRerankResponse, RawRerankDocument

StructuredResponseT = TypeVar("StructuredResponseT", bound=BaseModel)


@runtime_checkable
class HarborChatClientProtocol(Protocol):
    """Define the stable synchronous chat-client boundary."""

    def chat(
        self,
        messages: Sequence[HarborChatMessage] | None = None,
        *,
        request: HarborChatRequest | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> HarborChatResponse:
        """Generate one normalized chat response."""
        ...

    def stream(
        self,
        messages: Sequence[HarborChatMessage] | None = None,
        *,
        request: HarborChatRequest | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> Iterator[HarborChatStreamChunk]:
        """Generate normalized chat stream events."""
        ...

    def chat_structured(
        self,
        messages: Sequence[HarborChatMessage] | None = None,
        *,
        response_model: type[StructuredResponseT],
        request: HarborChatRequest | None = None,
        model: str | None = None,
        max_repair_attempts: int | None = None,
        **kwargs: Any,
    ) -> StructuredResponseT:
        """Generate and validate one typed structured response."""
        ...

    def close(self) -> None:
        """Release resources owned by the client."""
        ...


@runtime_checkable
class AsyncHarborChatClientProtocol(Protocol):
    """Define the stable asynchronous chat-client boundary."""

    async def achat(
        self,
        messages: Sequence[HarborChatMessage] | None = None,
        *,
        request: HarborChatRequest | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> HarborChatResponse:
        """Generate one normalized chat response."""
        ...

    def astream(
        self,
        messages: Sequence[HarborChatMessage] | None = None,
        *,
        request: HarborChatRequest | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[HarborChatStreamChunk]:
        """Generate normalized asynchronous chat stream events."""
        ...

    async def achat_structured(
        self,
        messages: Sequence[HarborChatMessage] | None = None,
        *,
        response_model: type[StructuredResponseT],
        request: HarborChatRequest | None = None,
        model: str | None = None,
        max_repair_attempts: int | None = None,
        **kwargs: Any,
    ) -> StructuredResponseT:
        """Generate and validate one typed asynchronous structured response."""
        ...

    async def aclose(self) -> None:
        """Release resources owned by the client."""
        ...


@runtime_checkable
class HarborEmbedClientProtocol(Protocol):
    """Define the stable synchronous embedding-client boundary."""

    def embed(
        self,
        inputs: RawEmbeddingInput | None = None,
        *,
        request: HarborEmbedRequest | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> HarborEmbedResponse:
        """Generate normalized embeddings."""
        ...

    def close(self) -> None:
        """Release resources owned by the client."""
        ...


@runtime_checkable
class AsyncHarborEmbedClientProtocol(Protocol):
    """Define the stable asynchronous embedding-client boundary."""

    async def aembed(
        self,
        inputs: RawEmbeddingInput | None = None,
        *,
        request: HarborEmbedRequest | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> HarborEmbedResponse:
        """Generate normalized embeddings."""
        ...

    async def aclose(self) -> None:
        """Release resources owned by the client."""
        ...


@runtime_checkable
class HarborRerankingClientProtocol(Protocol):
    """Define the stable synchronous reranking-client boundary."""

    def rerank(
        self,
        query: str | None = None,
        documents: Sequence[RawRerankDocument] | None = None,
        *,
        request: HarborRerankRequest | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> HarborRerankResponse:
        """Score and order a candidate document set."""
        ...

    def close(self) -> None:
        """Release resources owned by the client."""
        ...


@runtime_checkable
class AsyncHarborRerankingClientProtocol(Protocol):
    """Define the stable asynchronous reranking-client boundary."""

    async def arerank(
        self,
        query: str | None = None,
        documents: Sequence[RawRerankDocument] | None = None,
        *,
        request: HarborRerankRequest | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> HarborRerankResponse:
        """Score and order a candidate document set."""
        ...

    async def aclose(self) -> None:
        """Release resources owned by the client."""
        ...
