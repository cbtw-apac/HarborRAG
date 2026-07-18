from __future__ import annotations

import asyncio
from typing import Any

import pytest
from harborrag_adapters.models.chat import HarborChatClient
from harborrag_core.models.chat import HarborChatMessage, StreamEventType
from harborrag_core.models.errors import (
    HarborChatConnectionError,
    HarborChatProviderError,
)

from .chat_client_support import (
    FakeAsyncStream,
    FakeInvocation,
    FakeSyncStream,
    stream_chunk,
)


class BlockingAsyncStream:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = False

    def __aiter__(self) -> BlockingAsyncStream:
        return self

    async def __anext__(self) -> Any:
        self.started.set()
        await asyncio.Future()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.closed = True


def test_sync_text_stream_emits_usage_completion_and_closes(base_config) -> None:
    raw = FakeSyncStream(
        [
            stream_chunk("Harbor "),
            stream_chunk("RAG"),
            stream_chunk(finish_reason="stop"),
            stream_chunk(usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}),
        ]
    )
    invocation = FakeInvocation(streams=[raw])

    events = list(
        HarborChatClient(base_config, invocation=invocation).stream(
            [HarborChatMessage.user("hello")]
        )
    )

    assert [event.event for event in events] == [
        StreamEventType.METADATA,
        StreamEventType.TEXT_DELTA,
        StreamEventType.TEXT_DELTA,
        StreamEventType.METADATA,
        StreamEventType.USAGE,
        StreamEventType.COMPLETED,
    ]
    assert "".join(event.text_delta or "" for event in events) == "Harbor RAG"
    assert events[-2].usage.total_tokens == 6
    assert events[-1].finish_reason == "stop"
    assert events[-1].usage.total_tokens == 6
    assert invocation.stream_calls[0]["stream"] is True
    assert invocation.stream_calls[0]["stream_options"] == {"include_usage": True}
    assert raw.closed
    assert invocation.close_stream_count == 1


@pytest.mark.asyncio
async def test_async_text_stream_emits_normalized_events(base_config) -> None:
    raw = FakeAsyncStream(
        [
            stream_chunk("async "),
            stream_chunk("stream"),
            stream_chunk(finish_reason="length"),
        ]
    )
    invocation = FakeInvocation(async_streams=[raw])

    events = [
        event
        async for event in HarborChatClient(base_config, invocation=invocation).astream(
            [HarborChatMessage.user("hello")]
        )
    ]

    assert "".join(event.text_delta or "" for event in events) == "async stream"
    assert events[-1].event is StreamEventType.COMPLETED
    assert events[-1].finish_reason == "length"
    assert raw.closed
    assert invocation.aclose_stream_count == 1


def test_parallel_tool_call_stream_is_assembled_and_parsed(base_config) -> None:
    raw = FakeSyncStream(
        [
            stream_chunk(
                tool_calls=[
                    {
                        "index": 0,
                        "id": "call-weather",
                        "type": "function",
                        "function": {"name": "weather", "arguments": '{"city":"'},
                    },
                    {
                        "index": 1,
                        "id": "call-time",
                        "type": "function",
                        "function": {"name": "time", "arguments": '{"zone":"'},
                    },
                ]
            ),
            stream_chunk(
                tool_calls=[
                    {"index": 0, "function": {"arguments": 'Paris"}'}},
                    {"index": 1, "function": {"arguments": 'UTC"}'}},
                ]
            ),
            stream_chunk(finish_reason="tool_calls"),
        ]
    )

    events = list(
        HarborChatClient(base_config, invocation=FakeInvocation(streams=[raw])).stream(
            [HarborChatMessage.user("weather and time")]
        )
    )

    deltas = [
        event.tool_call_delta for event in events if event.event is StreamEventType.TOOL_CALL_DELTA
    ]
    assert [delta.index for delta in deltas] == [0, 1, 0, 1]
    completed = events[-1]
    assert completed.event is StreamEventType.COMPLETED
    assert tuple(call.id for call in completed.tool_calls) == (
        "call-weather",
        "call-time",
    )
    assert completed.tool_calls[0].function.parsed_arguments == {"city": "Paris"}
    assert completed.tool_calls[1].function.parsed_arguments == {"zone": "UTC"}


def test_provider_disconnect_emits_error_then_raises_and_closes(base_config) -> None:
    raw = FakeSyncStream([stream_chunk("partial"), ConnectionError("provider disconnected")])
    iterator = HarborChatClient(base_config, invocation=FakeInvocation(streams=[raw])).stream(
        [HarborChatMessage.user("hello")]
    )

    assert next(iterator).event is StreamEventType.METADATA
    assert next(iterator).event is StreamEventType.TEXT_DELTA
    error_event = next(iterator)
    assert error_event.event is StreamEventType.ERROR
    assert error_event.error["type"] == "HarborChatConnectionError"
    with pytest.raises(HarborChatConnectionError, match="provider request failed"):
        next(iterator)
    assert raw.closed


@pytest.mark.asyncio
async def test_exception_during_async_iteration_is_mapped_and_closed(
    base_config,
) -> None:
    raw = FakeAsyncStream([RuntimeError("broken stream")])
    iterator = HarborChatClient(
        base_config, invocation=FakeInvocation(async_streams=[raw])
    ).astream([HarborChatMessage.user("hello")])

    error_event = await anext(iterator)
    assert error_event.event is StreamEventType.ERROR
    with pytest.raises(HarborChatProviderError, match="provider request failed"):
        await anext(iterator)
    assert raw.closed


@pytest.mark.asyncio
async def test_async_stream_cancellation_closes_provider_resource(base_config) -> None:
    raw = BlockingAsyncStream()
    iterator = HarborChatClient(
        base_config, invocation=FakeInvocation(async_streams=[raw])
    ).astream([HarborChatMessage.user("hello")])
    pending = asyncio.create_task(anext(iterator))
    await raw.started.wait()

    pending.cancel()

    with pytest.raises(asyncio.CancelledError):
        await pending
    assert raw.closed


@pytest.mark.asyncio
async def test_consumers_can_close_streams_early(base_config) -> None:
    sync_raw = FakeSyncStream([stream_chunk("first"), stream_chunk("second")])
    sync_iterator = HarborChatClient(
        base_config, invocation=FakeInvocation(streams=[sync_raw])
    ).stream([HarborChatMessage.user("hello")])
    assert next(sync_iterator).event is StreamEventType.METADATA
    assert next(sync_iterator).text_delta == "first"
    sync_iterator.close()

    async_raw = FakeAsyncStream([stream_chunk("first"), stream_chunk("second")])
    async_iterator = HarborChatClient(
        base_config, invocation=FakeInvocation(async_streams=[async_raw])
    ).astream([HarborChatMessage.user("hello")])
    assert (await anext(async_iterator)).event is StreamEventType.METADATA
    assert (await anext(async_iterator)).text_delta == "first"
    await async_iterator.aclose()

    assert sync_raw.closed
    assert async_raw.closed
