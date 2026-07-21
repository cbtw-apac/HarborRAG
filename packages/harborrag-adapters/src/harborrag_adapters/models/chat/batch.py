from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from harborrag_core.models.chat import HarborChatRequest, HarborChatResponse
from harborrag_core.models.errors import HarborChatInvalidRequestError


class BatchFailureMode(StrEnum):
    """Control whether a chat batch stops or records independent item failures."""

    FAIL_FAST = "fail_fast"
    COLLECT = "collect"


@dataclass(frozen=True, slots=True)
class HarborChatBatchItem:
    """Store one ordered batch result or its item-specific exception."""

    index: int
    response: HarborChatResponse | None = None
    error: Exception | None = None

    @property
    def succeeded(self) -> bool:
        """Return whether this item completed with a normalized response."""

        return self.response is not None and self.error is None


@dataclass(frozen=True, slots=True)
class HarborChatBatchResult:
    """Return ordered item outcomes for collect-mode batch execution."""

    items: tuple[HarborChatBatchItem, ...]

    @property
    def responses(self) -> tuple[HarborChatResponse, ...]:
        """Return successful responses in original request order."""

        return tuple(item.response for item in self.items if item.response is not None)

    @property
    def errors(self) -> tuple[Exception, ...]:
        """Return item failures in original request order."""

        return tuple(item.error for item in self.items if item.error is not None)


class BatchChatClient(Protocol):
    """Define the chat operations required by the bounded batch executor."""

    def chat(self, *, request: HarborChatRequest) -> HarborChatResponse:
        """Generate one synchronous response."""

        ...

    async def achat(self, *, request: HarborChatRequest) -> HarborChatResponse:
        """Generate one asynchronous response."""

        ...


class ChatBatchExecutor:
    """Execute independent chat requests with bounded concurrency and stable ordering."""

    def __init__(self, client: BatchChatClient) -> None:
        """Store the client used for each independently routed request."""

        self._client = client

    def run(
        self,
        requests: tuple[HarborChatRequest, ...],
        *,
        concurrency: int,
        failure_mode: BatchFailureMode,
    ) -> HarborChatBatchResult:
        """Execute synchronous requests through a bounded worker pool."""

        _validate_batch(requests, concurrency)
        results: list[HarborChatBatchItem | None] = [None] * len(requests)
        with ThreadPoolExecutor(
            max_workers=min(concurrency, len(requests)),
            thread_name_prefix="harbor-chat-batch",
        ) as executor:
            futures = {
                executor.submit(self._client.chat, request=request): index
                for index, request in enumerate(requests)
            }
            self._collect_sync(futures, results, failure_mode)
        return HarborChatBatchResult(items=_complete_items(results))

    async def arun(
        self,
        requests: tuple[HarborChatRequest, ...],
        *,
        concurrency: int,
        failure_mode: BatchFailureMode,
    ) -> HarborChatBatchResult:
        """Execute asynchronous requests with a cancellation-safe semaphore."""

        _validate_batch(requests, concurrency)
        semaphore = asyncio.Semaphore(concurrency)

        async def execute(index: int, request: HarborChatRequest) -> HarborChatBatchItem:
            async with semaphore:
                try:
                    response = await self._client.achat(request=request)
                except Exception as exc:
                    if failure_mode is BatchFailureMode.FAIL_FAST:
                        raise
                    return HarborChatBatchItem(index=index, error=exc)
                return HarborChatBatchItem(index=index, response=response)

        tasks = [
            asyncio.create_task(execute(index, request)) for index, request in enumerate(requests)
        ]
        try:
            items = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return HarborChatBatchResult(items=tuple(sorted(items, key=lambda item: item.index)))

    @staticmethod
    def _collect_sync(
        futures: dict[Future[HarborChatResponse], int],
        results: list[HarborChatBatchItem | None],
        failure_mode: BatchFailureMode,
    ) -> None:
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = HarborChatBatchItem(index=index, response=future.result())
            except Exception as exc:
                if failure_mode is BatchFailureMode.FAIL_FAST:
                    for pending in futures:
                        pending.cancel()
                    raise
                results[index] = HarborChatBatchItem(index=index, error=exc)


def _validate_batch(requests: tuple[HarborChatRequest, ...], concurrency: int) -> None:
    if not requests:
        raise HarborChatInvalidRequestError(
            "chat batch requires at least one request",
            operation="chat",
            retryable=False,
        )
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
        raise HarborChatInvalidRequestError(
            "batch concurrency must be a positive integer",
            operation="chat",
            retryable=False,
        )


def _complete_items(
    results: list[HarborChatBatchItem | None],
) -> tuple[HarborChatBatchItem, ...]:
    if any(item is None for item in results):
        raise RuntimeError("chat batch terminated without completing every item")
    return tuple(item for item in results if item is not None)
