from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from harborrag_adapters.models.chat import HarborChatClient
from harborrag_adapters.models.chat.configs import HarborChatProviderConfig
from harborrag_adapters.models.chat.registry import HarborProvider
from harborrag_adapters.models.common.config import (
    CacheConfig,
    RetryPolicyConfig,
)
from harborrag_adapters.models.common.lifecycle import ResourceOwnership
from harborrag_core.models.capabilities import HarborChatCapabilities
from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatRequest,
    StreamEventType,
)
from harborrag_core.models.errors import (
    HarborChatProviderError,
    HarborChatStructuredOutputError,
    HarborChatTimeoutError,
)
from model_runtime_support import FakeChatInvocation, chat_config
from pydantic import BaseModel


class Answer(BaseModel):
    """Represent a structured answer returned by client tests."""

    answer: str


def raw_chat(content: str | None = "ok", *, finish: str = "stop") -> dict[str, Any]:
    """Build a complete LiteLLM-style chat response."""
    return {
        "id": "response",
        "model": "provider-model",
        "choices": [
            {"finish_reason": finish, "message": {"role": "assistant", "content": content}}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    }


def chunk(content: str | None = None, *, finish: str | None = None) -> dict[str, Any]:
    """Build one LiteLLM-style chat stream chunk."""
    return {
        "id": "stream-id",
        "model": "provider-model",
        "choices": [{"delta": {"content": content}, "finish_reason": finish}],
    }


def deployment(name: str, *, order: int = 0) -> HarborChatProviderConfig:
    """Build a compatible chat deployment for routing tests."""
    return HarborChatProviderConfig(
        name=name,
        provider=HarborProvider.OPENAI,
        model=f"openai/{name}",
        api_key="secret",
        order=order,
        capabilities=HarborChatCapabilities(
            structured_output=True,
            json_mode=True,
            tools=True,
            streaming=True,
        ),
    )


def test_chat_client_sync_async_cache_middleware_and_lifecycle() -> None:
    events: list[str] = []

    class Middleware:
        def before_request(self, request: Any, context: Any) -> Any:
            events.append("before")
            return request

        def after_response(self, response: Any, context: Any) -> Any:
            events.append("after")
            return response

    invocation = FakeChatInvocation([raw_chat("sync"), raw_chat("async")])
    config = chat_config(cache=CacheConfig(enabled=True, ttl_seconds=30))
    client = HarborChatClient.from_config(config, invocation=invocation, middleware=(Middleware(),))
    request = HarborChatRequest(
        messages=(HarborChatMessage.user("hello"),),
        metadata={"tenant_id": "tenant"},
        cacheable=True,
    )
    first = client.chat(request=request)
    cached = client.chat(request=request)
    assert first.text == "sync" and cached.cache_hit
    assert len(invocation.calls) == 1
    assert events == ["before", "after", "before", "after"]

    async def run() -> None:
        response = await client.achat([HarborChatMessage.user("other")])
        assert response.text == "async"

    import asyncio

    asyncio.run(run())
    assert invocation.calls[-1]["metadata"]["harborrag"]["operation"] == "chat"
    assert client is client.__enter__()
    client.close()
    client.close()
    assert invocation.closed == 1
    with pytest.raises(RuntimeError, match="closed"):
        client.chat([HarborChatMessage.user("x")])


@pytest.mark.asyncio
async def test_chat_client_async_context_closes_owned_resources() -> None:
    invocation = FakeChatInvocation([raw_chat()])
    async with HarborChatClient(chat_config(), invocation=invocation) as client:
        assert (await client.achat([HarborChatMessage.user("x")])).text == "ok"
    assert invocation.closed == 1
    with pytest.raises(RuntimeError, match="closed"):
        await client.achat([HarborChatMessage.user("x")])


def test_chat_retry_deployment_failover_and_model_fallback() -> None:
    retry = RetryPolicyConfig(
        same_deployment_attempts=1,
        max_deployment_failovers=1,
        max_model_fallbacks=1,
        base_delay_seconds=0,
        max_delay_seconds=0,
    )
    deployments = (deployment("first", order=0), deployment("second", order=1))
    invocation = FakeChatInvocation([TimeoutError("secret prompt"), raw_chat("second")])
    response = HarborChatClient(
        chat_config(deployments=deployments, retry=retry), invocation=invocation
    ).chat([HarborChatMessage.user("x")])
    assert response.text == "second"
    assert response.fallback_count == 1
    assert invocation.calls[0]["model"] == "openai/first"
    assert invocation.calls[1]["model"] == "openai/second"

    fallback_invocation = FakeChatInvocation([TimeoutError(), raw_chat("fallback")])
    fallback = HarborChatClient(
        chat_config(deployments=(deployment("only"),), retry=retry, fallbacks=("fallback",)),
        invocation=fallback_invocation,
    ).chat([HarborChatMessage.user("x")])
    assert fallback.text == "fallback" and fallback.fallback_count == 1
    assert fallback.logical_model == "fallback"


def test_chat_same_deployment_retry_and_nonretryable_error() -> None:
    retry = RetryPolicyConfig(
        same_deployment_attempts=2,
        max_deployment_failovers=0,
        max_model_fallbacks=0,
        base_delay_seconds=0,
        max_delay_seconds=0,
    )
    invocation = FakeChatInvocation([TimeoutError(), raw_chat("retried")])
    response = HarborChatClient(chat_config(retry=retry), invocation=invocation).chat(
        [HarborChatMessage.user("x")]
    )
    assert response.text == "retried" and response.retry_count == 1
    error = HarborChatProviderError("safe", retryable=False)
    failed = FakeChatInvocation([error, raw_chat("unused")])
    with pytest.raises(HarborChatProviderError):
        HarborChatClient(chat_config(retry=retry), invocation=failed).chat(
            [HarborChatMessage.user("x")]
        )
    assert len(failed.calls) == 1


def test_stream_retries_before_first_event_and_commits_after_output() -> None:
    invocation = FakeChatInvocation(
        [
            TimeoutError(),
            [
                chunk("hello"),
                chunk(None, finish="stop"),
                {"choices": [], "usage": {"total_tokens": 3}},
            ],
        ]
    )
    events = list(
        HarborChatClient(chat_config(), invocation=invocation).stream([HarborChatMessage.user("x")])
    )
    assert [event.event for event in events] == [
        StreamEventType.METADATA,
        StreamEventType.TEXT_DELTA,
        StreamEventType.METADATA,
        StreamEventType.USAGE,
        StreamEventType.COMPLETED,
    ]
    assert events[1].text_delta == "hello"
    assert events[-1].finish_reason == "stop"
    assert invocation.streams_closed == 1

    def partial() -> Iterator[dict[str, Any]]:
        yield chunk("partial")
        raise TimeoutError("disconnect")

    partial_invocation = FakeChatInvocation([partial(), [chunk("must-not-run")]])
    iterator = HarborChatClient(chat_config(), invocation=partial_invocation).stream(
        [HarborChatMessage.user("x")]
    )
    assert next(iterator).event is StreamEventType.METADATA
    assert next(iterator).text_delta == "partial"
    error_event = next(iterator)
    assert error_event.event is StreamEventType.ERROR
    with pytest.raises(HarborChatTimeoutError):
        next(iterator)
    assert len(partial_invocation.calls) == 1


@pytest.mark.asyncio
async def test_async_stream_normalization_and_cleanup() -> None:
    invocation = FakeChatInvocation([[chunk("a"), chunk("b", finish="stop")]])
    client = HarborChatClient(chat_config(), invocation=invocation)
    events = [event async for event in client.astream([HarborChatMessage.user("x")])]
    assert "".join(event.text_delta or "" for event in events) == "ab"
    assert events[-1].event is StreamEventType.COMPLETED
    assert invocation.streams_closed == 1


def test_structured_output_success_repair_and_terminal_failure() -> None:
    direct = HarborChatClient(
        chat_config(), invocation=FakeChatInvocation([raw_chat('{"answer":"yes"}')])
    )
    assert (
        direct.chat_structured([HarborChatMessage.user("x")], response_model=Answer).answer == "yes"
    )
    repairing = FakeChatInvocation([raw_chat("not json"), raw_chat('{"answer":"fixed"}')])
    fixed = HarborChatClient(chat_config(), invocation=repairing).chat_structured(
        [HarborChatMessage.user("x")], response_model=Answer, max_repair_attempts=1
    )
    assert fixed.answer == "fixed"
    assert len(repairing.calls[1]["messages"]) > len(repairing.calls[0]["messages"])
    with pytest.raises(HarborChatStructuredOutputError):
        HarborChatClient(
            chat_config(), invocation=FakeChatInvocation([raw_chat("bad")])
        ).chat_structured(
            [HarborChatMessage.user("x")], response_model=Answer, max_repair_attempts=0
        )


@pytest.mark.asyncio
async def test_async_structured_output_and_borrowed_invocation() -> None:
    invocation = FakeChatInvocation([raw_chat('{"answer":"async"}')])
    client = HarborChatClient(
        chat_config(), invocation=invocation, resource_ownership=ResourceOwnership.BORROWED
    )
    result = await client.achat_structured([HarborChatMessage.user("x")], response_model=Answer)
    assert result.answer == "async"
    await client.aclose()
    assert invocation.closed == 0
