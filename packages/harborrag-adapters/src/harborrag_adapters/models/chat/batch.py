from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Protocol

from harborrag_core.models.chat import HarborChatRequest, HarborChatResponse
from harborrag_core.models.errors import HarborChatInvalidRequestError

from .schemas import (
    BatchFailureMode,
    HarborChatBatchItem,
    HarborChatBatchResult,
)


class SyncBatchChatClient(Protocol):
    """Define the synchronous operation required by batch execution."""

    def chat(self, *, request: HarborChatRequest) -> HarborChatResponse: ...


class AsyncBatchChatClient(Protocol):
    """Define the asynchronous operation required by batch execution."""

    async def achat(self, *, request: HarborChatRequest) -> HarborChatResponse: ...


class SyncChatBatchExecutor:
    """Execute synchronous requests with bounded concurrency and stable ordering."""

    def __init__(self, client: SyncBatchChatClient) -> None:
        self._client = client

    def run(
        self,
        requests: tuple[HarborChatRequest, ...],
        *,
        concurrency: int,
        failure_mode: BatchFailureMode,
    ) -> HarborChatBatchResult:
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
            self._collect(futures, results, failure_mode)
        return HarborChatBatchResult(items=_complete_items(results))

    @staticmethod
    def _collect(
        futures: dict[Future[HarborChatResponse], int],
        results: list[HarborChatBatchItem | None],
        failure_mode: BatchFailureMode,
    ) -> None:
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = HarborChatBatchItem(
                    index=index,
                    response=future.result(),
                )
            except Exception as exc:
                if failure_mode is BatchFailureMode.FAIL_FAST:
                    for pending in futures:
                        pending.cancel()
                    raise
                results[index] = HarborChatBatchItem(index=index, error=exc)


class AsyncChatBatchExecutor:
    """Execute asynchronous requests with bounded concurrency and stable ordering."""

    def __init__(self, client: AsyncBatchChatClient) -> None:
        self._client = client

    async def run(
        self,
        requests: tuple[HarborChatRequest, ...],
        *,
        concurrency: int,
        failure_mode: BatchFailureMode,
    ) -> HarborChatBatchResult:
        _validate_batch(requests, concurrency)
        semaphore = asyncio.Semaphore(concurrency)

        async def execute(
            index: int,
            request: HarborChatRequest,
        ) -> HarborChatBatchItem:
            async with semaphore:
                try:
                    response = await self._client.achat(request=request)
                except Exception as exc:
                    if failure_mode is BatchFailureMode.FAIL_FAST:
                        raise
                    return HarborChatBatchItem(index=index, error=exc)
                return HarborChatBatchItem(index=index, response=response)

        tasks = [
            asyncio.create_task(execute(index, request))
            for index, request in enumerate(requests)
        ]
        try:
            items = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return HarborChatBatchResult(
            items=tuple(sorted(items, key=lambda item: item.index))
        )


def _validate_batch(
    requests: tuple[HarborChatRequest, ...],
    concurrency: int,
) -> None:
    if not requests:
        raise HarborChatInvalidRequestError(
            "chat batch requires at least one request",
            operation="chat",
            retryable=False,
        )
    if (
        isinstance(concurrency, bool)
        or not isinstance(concurrency, int)
        or concurrency < 1
    ):
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
