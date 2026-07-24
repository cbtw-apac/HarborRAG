from __future__ import annotations

import hashlib

import pytest
from chat.chat_client_support import (
    FakeInvocation,
    FakeSyncStream,
    response_dict,
    stream_chunk,
)
from telemetry_support import (
    FailingTelemetry,
    RecordingTelemetry,
)
from telemetry_support import chat_config as _config
from telemetry_support import recorded_event as _event
from telemetry_support import telemetry_dispatcher as _dispatcher

from harborrag_adapters.models.chat import HarborChatClient
from harborrag_adapters.models.common.config import (
    ObservabilityConfig,
    TelemetryFailureMode,
)
from harborrag_adapters.models.common.lifecycle import ResourceOwnership
from harborrag_adapters.models.common.security import PrivacyConfig
from harborrag_adapters.models.common.telemetry import (
    TelemetryDispatcher,
    TelemetryDispatchError,
    TelemetryEventType,
)
from harborrag_core.models.chat import HarborChatMessage
from harborrag_core.models.errors import (
    HarborChatConnectionError,
    HarborChatProviderError,
)

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def test_request_lifecycle_captures_identity_usage_and_safe_defaults() -> None:
    sink = RecordingTelemetry()
    privacy = PrivacyConfig(metadata_allowlist=frozenset({"collection_name"}))
    invocation = FakeInvocation([response_dict()])
    client = HarborChatClient(
        _config(), invocation=invocation, telemetry=_dispatcher(sink, privacy)
    )

    response = client.chat(
        [HarborChatMessage.user("private prompt")],
        model="friendly-chat",
        metadata={
            "request_id": "request-1",
            "trace_id": "trace-1",
            "workflow_id": "workflow-1",
            "tenant_id": "tenant-1",
            "user_id": "user-1",
            "collection_name": "manuals",
            "unapproved": "drop-me",
        },
    )

    started = _event(sink, TelemetryEventType.REQUEST_START)
    completed = _event(sink, TelemetryEventType.REQUEST_COMPLETE)
    assert started.request_id == response.request_id == "request-1"
    assert (started.trace_id, started.workflow_id) == ("trace-1", "workflow-1")
    assert started.tenant_id == hashlib.sha256(b"tenant-1").hexdigest()
    assert started.user_id == hashlib.sha256(b"user-1").hexdigest()
    assert started.metadata == {"collection_name": "manuals"}
    assert started.model_alias == "friendly-chat"
    assert started.input_payload is None
    assert completed.output_payload is None
    assert completed.provider == "openai"
    assert completed.provider_model == "gpt-test"
    assert completed.usage["total_tokens"] == 5
    assert completed.total_duration_ms is not None
    assert invocation.calls[0]["metadata"] == {
        "harborrag": {
            "collection_name": "manuals",
            "logical_model": "primary",
            "operation": "chat",
            "request_id": "request-1",
            "tenant_id": started.tenant_id,
            "trace_id": "trace-1",
            "user_id": started.user_id,
            "workflow_id": "workflow-1",
        },
        "trace_id": "trace-1",
    }


@pytest.mark.asyncio
async def test_async_request_dispatches_start_and_completion() -> None:
    sink = RecordingTelemetry()
    client = HarborChatClient(
        _config(),
        invocation=FakeInvocation([response_dict()]),
        telemetry=_dispatcher(sink),
    )

    response = await client.achat([HarborChatMessage.user("hello")])

    assert response.text == "hello"
    assert [event.event_type for event in sink.events] == [
        TelemetryEventType.REQUEST_START,
        TelemetryEventType.CACHE_BYPASS,
        TelemetryEventType.REQUEST_COMPLETE,
    ]


def test_prompt_and_response_content_are_redacted_and_bounded() -> None:
    sink = RecordingTelemetry()
    privacy = PrivacyConfig(
        log_inputs=True,
        log_outputs=True,
        redact_fields=frozenset({"content"}),
        max_logged_content_length=10_000,
    )
    client = HarborChatClient(
        _config(),
        invocation=FakeInvocation([response_dict("private response")]),
        telemetry=_dispatcher(sink, privacy),
    )

    client.chat([HarborChatMessage.user("private prompt")])

    started = _event(sink, TelemetryEventType.REQUEST_START)
    completed = _event(sink, TelemetryEventType.REQUEST_COMPLETE)
    assert "private prompt" not in str(started.input_payload)
    assert "[REDACTED]" in str(started.input_payload)
    assert "private response" not in str(completed.output_payload)
    assert "[REDACTED]" in str(completed.output_payload)

    bounded = _dispatcher(
        RecordingTelemetry(),
        PrivacyConfig(log_inputs=True, max_logged_content_length=20),
    ).privacy.content({"prompt": "x" * 200})
    assert isinstance(bounded, str)
    assert len(bounded) == 20


def test_tool_calls_capture_names_and_redact_json_arguments() -> None:
    sink = RecordingTelemetry()
    raw = response_dict(None, finish_reason="tool_calls")
    raw["choices"][0]["message"]["tool_calls"] = [
        {
            "id": "call-1",
            "type": "function",
            "function": {
                "name": "lookup",
                "arguments": '{"topic":"harbor","api_key":"top-secret"}',
            },
        }
    ]
    privacy = PrivacyConfig(log_outputs=True, max_logged_content_length=10_000)
    client = HarborChatClient(
        _config(),
        invocation=FakeInvocation([raw]),
        telemetry=_dispatcher(sink, privacy),
    )

    client.chat([HarborChatMessage.user("hello")])

    completed = _event(sink, TelemetryEventType.REQUEST_COMPLETE)
    assert completed.tool_calls[0]["name"] == "lookup"
    assert completed.tool_calls[0]["arguments"] == {
        "topic": "harbor",
        "api_key": "[REDACTED]",
    }
    assert "top-secret" not in str(completed.output_payload)


def test_retry_deployment_and_model_fallback_events_are_distinct() -> None:
    sink = RecordingTelemetry()
    invocation = FakeInvocation(
        [
            TimeoutError("one"),
            TimeoutError("two"),
            TimeoutError("three"),
            TimeoutError("four"),
            response_dict(),
        ]
    )
    client = HarborChatClient(
        _config(deployments=2, fallback=True, attempts=2),
        invocation=invocation,
        telemetry=_dispatcher(sink),
    )

    response = client.chat([HarborChatMessage.user("hello")])

    event_types = [event.event_type for event in sink.events]
    assert TelemetryEventType.RETRY in event_types
    assert TelemetryEventType.DEPLOYMENT_FALLBACK in event_types
    assert TelemetryEventType.MODEL_FALLBACK in event_types
    assert response.retry_count == 2
    assert response.fallback_count == 2


def test_cache_events_report_miss_then_hit() -> None:
    sink = RecordingTelemetry()
    invocation = FakeInvocation([response_dict("cached")])
    client = HarborChatClient(
        _config(cache=True), invocation=invocation, telemetry=_dispatcher(sink)
    )
    request = [HarborChatMessage.user("hello")]
    kwargs = {"cacheable": True, "metadata": {"tenant_id": "tenant"}}

    first = client.chat(request, **kwargs)
    second = client.chat(request, **kwargs)

    cache_events = [event.event_type for event in sink.events if "cache" in event.event_type]
    assert cache_events == [TelemetryEventType.CACHE_MISS, TelemetryEventType.CACHE_HIT]
    assert not first.cache_hit and second.cache_hit
    assert len(invocation.calls) == 1


def test_errors_are_sanitized_before_dispatch() -> None:
    sink = RecordingTelemetry()
    client = HarborChatClient(
        _config(),
        invocation=FakeInvocation([RuntimeError("api_key=top-secret")]),
        telemetry=_dispatcher(sink),
    )

    with pytest.raises(HarborChatProviderError):
        client.chat([HarborChatMessage.user("hello")])

    error = _event(sink, TelemetryEventType.REQUEST_ERROR).error
    assert error is not None
    assert "top-secret" not in str(error)
    assert "provider request failed" in str(error).lower()


def test_stream_events_include_first_token_and_completion() -> None:
    sink = RecordingTelemetry()
    raw = FakeSyncStream([stream_chunk("hello"), stream_chunk(finish_reason="stop")])
    client = HarborChatClient(
        _config(),
        invocation=FakeInvocation(streams=[raw]),
        telemetry=_dispatcher(sink),
    )

    chunks = list(client.stream([HarborChatMessage.user("hello")]))

    event_types = [event.event_type for event in sink.events]
    assert event_types.count(TelemetryEventType.STREAM_EVENT) == len(chunks) - 1
    assert TelemetryEventType.STREAM_START in event_types
    assert TelemetryEventType.STREAM_COMPLETE in event_types
    assert TelemetryEventType.REQUEST_COMPLETE in event_types
    stream_event = next(
        event
        for event in sink.events
        if event.event_type is TelemetryEventType.STREAM_EVENT
        and event.first_token_latency_ms is not None
    )
    assert stream_event.first_token_latency_ms is not None
    assert stream_event.output_payload is None
    assert raw.closed


def test_stream_errors_emit_stream_and_request_failure_events() -> None:
    sink = RecordingTelemetry()
    raw = FakeSyncStream([stream_chunk("hello"), ConnectionError("disconnected")])
    client = HarborChatClient(
        _config(),
        invocation=FakeInvocation(streams=[raw]),
        telemetry=_dispatcher(sink),
    )

    with pytest.raises(HarborChatConnectionError, match="provider request failed"):
        list(client.stream([HarborChatMessage.user("hello")]))

    event_types = [event.event_type for event in sink.events]
    assert TelemetryEventType.STREAM_ERROR in event_types
    assert TelemetryEventType.REQUEST_ERROR in event_types
    assert raw.closed


def test_stream_event_suppression_retains_first_token_latency() -> None:
    sink = RecordingTelemetry()
    dispatcher = TelemetryDispatcher(
        [sink], config=ObservabilityConfig(include_stream_events=False)
    )
    client = HarborChatClient(
        _config(),
        invocation=FakeInvocation(
            streams=[FakeSyncStream([stream_chunk("hello"), stream_chunk(finish_reason="stop")])]
        ),
        telemetry=dispatcher,
    )

    list(client.stream([HarborChatMessage.user("hello")]))

    event_types = [event.event_type for event in sink.events]
    assert TelemetryEventType.STREAM_EVENT not in event_types
    completed = _event(sink, TelemetryEventType.STREAM_COMPLETE)
    assert completed.first_token_latency_ms is not None


@pytest.mark.parametrize(
    ("mode", "raises"),
    [
        (TelemetryFailureMode.IGNORE, False),
        (TelemetryFailureMode.RAISE, True),
    ],
)
def test_telemetry_failures_are_isolated_unless_strict(
    mode: TelemetryFailureMode, raises: bool
) -> None:
    invocation = FakeInvocation([response_dict()])
    config = ObservabilityConfig(failure_mode=mode)
    dispatcher = TelemetryDispatcher(
        [FailingTelemetry(TelemetryEventType.REQUEST_COMPLETE)], config=config
    )
    client = HarborChatClient(_config(), invocation=invocation, telemetry=dispatcher)

    if raises:
        with pytest.raises(TelemetryDispatchError):
            client.chat([HarborChatMessage.user("hello")])
    else:
        assert client.chat([HarborChatMessage.user("hello")]).text == "hello"

    assert len(invocation.calls) == 1


@pytest.mark.parametrize(
    ("ownership", "closed"),
    [
        (ResourceOwnership.BORROWED, False),
        (ResourceOwnership.OWNED, True),
    ],
)
def test_client_respects_injected_telemetry_ownership(
    ownership: ResourceOwnership, closed: bool
) -> None:
    sink = RecordingTelemetry()
    client = HarborChatClient(
        _config(),
        invocation=FakeInvocation(),
        telemetry=_dispatcher(sink),
        resource_ownership=ResourceOwnership.BORROWED,
        telemetry_ownership=ownership,
    )

    client.close()

    assert sink.closed is closed
