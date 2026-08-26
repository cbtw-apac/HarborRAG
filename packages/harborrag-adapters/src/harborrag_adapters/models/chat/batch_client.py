from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from harborrag_core.models.chat import HarborChatRequest, HarborChatResponse

from .batch import AsyncChatBatchExecutor, SyncChatBatchExecutor
from .schemas import BatchFailureMode, HarborChatBatchResult


class SyncChatBatchBoundary(Protocol):
    def chat(self, *, request: HarborChatRequest) -> HarborChatResponse: ...

    def _ensure_open(self) -> None: ...


class AsyncChatBatchBoundary(Protocol):
    async def achat(self, *, request: HarborChatRequest) -> HarborChatResponse: ...

    def _ensure_open(self) -> None: ...


class SyncChatBatchMixin:
    """Add bounded ordered batch operations to the synchronous client."""

    def chat_many(
        self: SyncChatBatchBoundary,
        requests: Sequence[HarborChatRequest],
        *,
        concurrency: int = 8,
        failure_mode: BatchFailureMode = BatchFailureMode.COLLECT,
    ) -> HarborChatBatchResult:
        self._ensure_open()
        return SyncChatBatchExecutor(self).run(
            tuple(requests),
            concurrency=concurrency,
            failure_mode=failure_mode,
        )


class AsyncChatBatchMixin:
    """Add bounded ordered batch operations to the asynchronous client."""

    async def achat_many(
        self: AsyncChatBatchBoundary,
        requests: Sequence[HarborChatRequest],
        *,
        concurrency: int = 8,
        failure_mode: BatchFailureMode = BatchFailureMode.COLLECT,
    ) -> HarborChatBatchResult:
        self._ensure_open()
        return await AsyncChatBatchExecutor(self).run(
            tuple(requests),
            concurrency=concurrency,
            failure_mode=failure_mode,
        )
