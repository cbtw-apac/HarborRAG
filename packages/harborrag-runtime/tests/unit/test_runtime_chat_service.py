"""Unit tests for RuntimeChatService lazy client lifecycle."""

from __future__ import annotations

import asyncio

import pytest

from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatRequest,
    HarborChatResponse,
    HarborChatUsage,
)
from harborrag_runtime.chat import RuntimeChatService
from harborrag_runtime.config.settings import RuntimeSettings


class _FakeChatClient:
    def __init__(self) -> None:
        self.closed = False

    async def achat(self, messages=None, *, request=None, model=None, **kwargs):
        del messages, model, kwargs
        return HarborChatResponse(
            id="chat-1",
            logical_model="primary",
            provider="mock",
            provider_model="mock-chat",
            deployment="mock-primary",
            message=HarborChatMessage.assistant("hello"),
            finish_reason="stop",
            usage=HarborChatUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    async def aclose(self) -> None:
        self.closed = True


def _request() -> HarborChatRequest:
    return HarborChatRequest(messages=(HarborChatMessage.user("hi"),))


@pytest.mark.asyncio
async def test_configured_client_is_built_once_and_reused() -> None:
    built: list[_FakeChatClient] = []

    def build(_settings: RuntimeSettings) -> _FakeChatClient:
        client = _FakeChatClient()
        built.append(client)
        return client

    service = RuntimeChatService(RuntimeSettings(), client_builder=build)

    await service.complete(_request())
    await service.complete(_request())

    assert len(built) == 1


@pytest.mark.asyncio
async def test_aclose_closes_the_configured_client() -> None:
    client = _FakeChatClient()
    service = RuntimeChatService(RuntimeSettings(), client_builder=lambda _settings: client)

    await service.complete(_request())
    await service.aclose()

    assert client.closed is True


@pytest.mark.asyncio
async def test_aclose_is_a_no_op_when_no_client_was_ever_built() -> None:
    service = RuntimeChatService(
        RuntimeSettings(), client_builder=lambda _settings: _FakeChatClient()
    )

    await service.aclose()  # must not raise


@pytest.mark.asyncio
async def test_aclose_serializes_against_concurrent_client_creation() -> None:
    # Without holding _client_lock across the whole close, a concurrent
    # _configured_client() call can see the field already nulled out and
    # build a second client that this aclose() call never closes -- a leak
    # under concurrent shutdown. If the lock is held for the full close, the
    # concurrent caller must block until aclose() has fully returned.
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    built: list[_FakeChatClient] = []

    class _SlowCloseClient(_FakeChatClient):
        async def aclose(self) -> None:
            close_started.set()
            await allow_close.wait()
            await super().aclose()

    def build(_settings: RuntimeSettings) -> _SlowCloseClient:
        client = _SlowCloseClient()
        built.append(client)
        return client

    service = RuntimeChatService(RuntimeSettings(), client_builder=build)
    await service.complete(_request())
    assert len(built) == 1

    close_task = asyncio.create_task(service.aclose())
    await close_started.wait()

    complete_task = asyncio.create_task(service.complete(_request()))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(built) == 1, "a concurrent caller must not build a second client mid-close"

    allow_close.set()
    await close_task
    await complete_task

    assert len(built) == 2
    assert built[0].closed is True
