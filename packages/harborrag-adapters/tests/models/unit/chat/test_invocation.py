from __future__ import annotations

from typing import Any

import pytest

from harborrag_adapters.models.chat.invocation import LiteLLMChatInvocation

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


class AsyncOnlyStream:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_invocation_forwards_sync_and_async_calls() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def completion(**kwargs: Any) -> str:
        calls.append(("sync", kwargs))
        return "sync-response"

    async def acompletion(**kwargs: Any) -> str:
        calls.append(("async", kwargs))
        return "async-response"

    invocation = LiteLLMChatInvocation(
        completion=completion,
        acompletion=acompletion,
    )

    assert invocation.complete(model="sync-model") == "sync-response"
    assert await invocation.acomplete(model="async-model") == "async-response"
    assert invocation.stream(model="sync-stream", stream=True) == "sync-response"
    assert await invocation.astream(model="async-stream", stream=True) == "async-response"
    assert calls == [
        ("sync", {"model": "sync-model"}),
        ("async", {"model": "async-model"}),
        ("sync", {"model": "sync-stream", "stream": True}),
        ("async", {"model": "async-stream", "stream": True}),
    ]
    assert invocation.close() is None
    assert await invocation.aclose() is None


@pytest.mark.asyncio
async def test_sync_cleanup_closes_litellm_async_only_streams() -> None:
    async_only = AsyncOnlyStream()
    invocation = LiteLLMChatInvocation(
        completion=lambda **_: None,
        acompletion=lambda **_: None,
    )

    invocation.close_stream(async_only)

    assert async_only.closed
