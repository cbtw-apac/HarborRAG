from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from typing import Any

from harborrag_adapters.models.chat import (
    AsyncHarborChatClient,
    ChatClientDependencies,
    HarborChatClient,
)
from harborrag_adapters.models.chat.configs import HarborChatClientConfig


def sync_client(
    config: HarborChatClientConfig,
    **dependencies: Any,
) -> HarborChatClient:
    return HarborChatClient(config, ChatClientDependencies(**dependencies))


def async_client(
    config: HarborChatClientConfig,
    **dependencies: Any,
) -> AsyncHarborChatClient:
    return AsyncHarborChatClient(config, ChatClientDependencies(**dependencies))


class FakeInvocation:
    def __init__(
        self,
        responses: list[Any] | None = None,
        *,
        streams: list[Any] | None = None,
        async_streams: list[Any] | None = None,
    ) -> None:
        self.responses = list(responses or [])
        self.streams = list(streams or [])
        self.async_streams = list(async_streams or [])
        self.calls: list[dict[str, Any]] = []
        self.async_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []
        self.async_stream_calls: list[dict[str, Any]] = []
        self.close_count = 0
        self.aclose_count = 0
        self.close_stream_count = 0
        self.aclose_stream_count = 0

    def complete(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._next()

    async def acomplete(self, **kwargs: Any) -> Any:
        self.async_calls.append(kwargs)
        value = self._next()
        if asyncio.iscoroutine(value):
            return await value
        return value

    def stream(self, **kwargs: Any) -> Any:
        self.stream_calls.append(kwargs)
        return self._next_from(self.streams, "stream")

    async def astream(self, **kwargs: Any) -> Any:
        self.async_stream_calls.append(kwargs)
        queue = self.async_streams or self.streams
        return self._next_from(queue, "async stream")

    def close_stream(self, stream: Any) -> None:
        self.close_stream_count += 1
        close = getattr(stream, "close", None)
        if callable(close):
            close()

    async def aclose_stream(self, stream: Any) -> None:
        self.aclose_stream_count += 1
        aclose = getattr(stream, "aclose", None)
        if callable(aclose):
            await aclose()
            return
        close = getattr(stream, "close", None)
        if callable(close):
            close()

    def close(self) -> None:
        self.close_count += 1

    async def aclose(self) -> None:
        self.aclose_count += 1

    def _next(self) -> Any:
        return self._next_from(self.responses, "response")

    def _next_from(self, queue: list[Any], kind: str) -> Any:
        if not queue:
            raise AssertionError(f"FakeInvocation has no queued {kind}")
        value = queue.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value() if callable(value) else value


class FakeSyncStream(Iterator[Any]):
    def __init__(self, items: list[Any]) -> None:
        self.items = iter(items)
        self.closed = False

    def __iter__(self) -> FakeSyncStream:
        return self

    def __next__(self) -> Any:
        value = next(self.items)
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self) -> None:
        self.closed = True


class FakeAsyncStream(AsyncIterator[Any]):
    def __init__(self, items: list[Any]) -> None:
        self.items = iter(items)
        self.closed = False

    def __aiter__(self) -> FakeAsyncStream:
        return self

    async def __anext__(self) -> Any:
        try:
            value = next(self.items)
        except StopIteration as exc:
            raise StopAsyncIteration from exc
        if isinstance(value, BaseException):
            raise value
        if asyncio.iscoroutine(value):
            return await value
        return value

    async def aclose(self) -> None:
        self.closed = True


def response_dict(
    text: str | None = "hello",
    *,
    model: str = "gpt-test",
    finish_reason: str = "stop",
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": "resp-1",
        "created": 1,
        "model": model,
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": text},
            }
        ],
        "usage": usage or {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


def stream_chunk(
    content: str | None = None,
    *,
    finish_reason: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    choices = (
        [{"delta": delta, "finish_reason": finish_reason}]
        if delta or finish_reason is not None
        else []
    )
    return {
        "id": "stream-1",
        "model": "gpt-test",
        "choices": choices,
        "usage": usage,
    }
