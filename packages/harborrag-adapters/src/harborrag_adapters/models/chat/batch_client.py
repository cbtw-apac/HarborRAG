from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from harborrag_core.models.chat import HarborChatRequest, HarborChatResponse

from .batch import (
    BatchFailureMode,
    ChatBatchExecutor,
    HarborChatBatchResult,
)


class ChatBatchClientBoundary(Protocol):
    """Describe the methods consumed by the public chat batch mixin."""

    def chat(self, *, request: HarborChatRequest) -> HarborChatResponse:
        """Generate one synchronous response."""

        ...

    async def achat(self, *, request: HarborChatRequest) -> HarborChatResponse:
        """Generate one asynchronous response."""

        ...

    def _ensure_open(self) -> None:
        """Reject operations after client shutdown."""

        ...


class HarborChatBatchMixin:
    """Add bounded ordered batch operations to a concrete Harbor chat client."""

    def chat_many(
        self: ChatBatchClientBoundary,
        requests: Sequence[HarborChatRequest],
        *,
        concurrency: int = 8,
        failure_mode: BatchFailureMode = BatchFailureMode.COLLECT,
    ) -> HarborChatBatchResult:
        """Execute independent synchronous chat requests with bounded concurrency."""

        self._ensure_open()
        return ChatBatchExecutor(self).run(
            tuple(requests),
            concurrency=concurrency,
            failure_mode=failure_mode,
        )

    async def achat_many(
        self: ChatBatchClientBoundary,
        requests: Sequence[HarborChatRequest],
        *,
        concurrency: int = 8,
        failure_mode: BatchFailureMode = BatchFailureMode.COLLECT,
    ) -> HarborChatBatchResult:
        """Execute independent asynchronous chat requests with bounded concurrency."""

        self._ensure_open()
        return await ChatBatchExecutor(self).arun(
            tuple(requests),
            concurrency=concurrency,
            failure_mode=failure_mode,
        )
