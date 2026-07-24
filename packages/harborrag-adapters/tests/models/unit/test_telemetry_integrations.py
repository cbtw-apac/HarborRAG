from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from harborrag_adapters.models.runtime.config import (
    ObservabilityConfig,
    TelemetryFailureMode,
)
from harborrag_adapters.models.runtime.litellm_telemetry import LiteLLMTelemetryCallback
from harborrag_adapters.models.runtime.security import PrivacyConfig
from harborrag_adapters.models.runtime.telemetry import (
    OperationStatus,
    TelemetryDispatcher,
    TelemetryDispatchError,
    TelemetryEvent,
    TelemetryEventType,
    disabled_telemetry,
    litellm_telemetry_metadata,
)
from harborrag_adapters.models.runtime.telemetry_adapters import (
    LangfuseTelemetry,
    OpenTelemetryTelemetry,
    StructuredLoggingTelemetry,
)

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def event(kind: TelemetryEventType, **updates: Any) -> TelemetryEvent:
    """Build one feature-rich telemetry event for adapter tests."""
    values: dict[str, Any] = {
        "event_type": kind,
        "operation": "chat",
        "status": OperationStatus.STARTED,
        "request_id": "request",
        "trace_id": "trace",
        "workflow_id": "workflow",
        "tenant_id": "tenant",
        "user_id": "user",
        "model_alias": "alias",
        "logical_model": "primary",
        "provider": "openai",
        "provider_model": "gpt",
        "deployment": "deployment",
        "latency_ms": 2,
        "streaming": False,
        "usage": {"total_tokens": 3},
        "estimated_cost_usd": 0.01,
        "metadata": {"pipeline_stage": "answer", "private": "drop"},
        "input_payload": {"prompt": "secret"},
        "output_payload": {"answer": "secret"},
        "tool_calls": ({"name": "tool", "arguments": '{"password":"x"}'},),
        "attributes": {"safe": True},
    }
    values.update(updates)
    return TelemetryEvent(**values)


class RecordingSink:
    """Record sync and async telemetry dispatch and lifecycle calls."""

    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[TelemetryEvent] = []
        self.closed = 0
        self.fail = fail

    def emit(self, item: TelemetryEvent) -> None:
        if self.fail:
            raise RuntimeError("sink")
        self.events.append(item)

    async def aemit(self, item: TelemetryEvent) -> None:
        self.emit(item)

    def close(self) -> None:
        self.closed += 1
        if self.fail:
            raise RuntimeError("close")

    async def aclose(self) -> None:
        self.close()


def test_dispatcher_sanitizes_payloads_identifiers_and_tools() -> None:
    sink = RecordingSink()
    dispatcher = TelemetryDispatcher((sink,), config=ObservabilityConfig())
    dispatcher.emit(event(TelemetryEventType.REQUEST_START))
    prepared = sink.events[0]
    assert prepared.input_payload is None and prepared.output_payload is None
    assert prepared.tenant_id != "tenant" and prepared.user_id != "user"
    assert prepared.metadata == {"pipeline_stage": "answer"}
    assert "arguments" not in prepared.tool_calls[0]
    assert dispatcher.enabled and dispatcher.include_stream_events
    dispatcher.close()
    assert sink.closed == 1


@pytest.mark.asyncio
async def test_dispatcher_async_payload_logging_and_disabled_mode() -> None:
    sink = RecordingSink()
    config = ObservabilityConfig(
        privacy=PrivacyConfig(
            log_inputs=True,
            log_outputs=True,
            hash_user_identifiers=False,
            metadata_allowlist=frozenset({"pipeline_stage"}),
        )
    )
    dispatcher = TelemetryDispatcher((sink,), config=config)
    await dispatcher.aemit(event(TelemetryEventType.REQUEST_COMPLETE))
    prepared = sink.events[0]
    assert prepared.input_payload == {"prompt": "secret"}
    assert prepared.output_payload == {"answer": "secret"}
    assert "[REDACTED]" in prepared.tool_calls[0]["arguments"]
    await dispatcher.aclose()
    assert sink.closed == 1
    disabled = disabled_telemetry()
    assert not disabled.enabled
    disabled.emit(event(TelemetryEventType.REQUEST_START))
    await disabled.aemit(event(TelemetryEventType.REQUEST_START))


def test_dispatcher_failure_modes_and_close_aggregation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failing = RecordingSink(fail=True)
    ignored = TelemetryDispatcher((failing,), failure_mode=TelemetryFailureMode.IGNORE)
    with caplog.at_level(logging.ERROR):
        ignored.emit(event(TelemetryEventType.REQUEST_START))
    assert "Telemetry sink" in caplog.text
    strict = TelemetryDispatcher((failing,), failure_mode=TelemetryFailureMode.RAISE)
    with pytest.raises(TelemetryDispatchError):
        strict.emit(event(TelemetryEventType.REQUEST_START))
    with pytest.raises(RuntimeError, match="close"):
        strict.close()
    two = TelemetryDispatcher(
        (RecordingSink(fail=True), RecordingSink(fail=True)),
        failure_mode=TelemetryFailureMode.RAISE,
    )
    with pytest.raises(ExceptionGroup):
        two.close()


@pytest.mark.asyncio
async def test_async_dispatch_fallback_and_strict_close() -> None:
    class SyncOnly:
        def __init__(self) -> None:
            self.events: list[TelemetryEvent] = []

        def emit(self, item: TelemetryEvent) -> None:
            self.events.append(item)

        def close(self) -> None:
            return None

    sync = SyncOnly()
    dispatcher = TelemetryDispatcher((sync,))
    await dispatcher.aemit(event(TelemetryEventType.REQUEST_START))
    assert len(sync.events) == 1
    await dispatcher.aclose()


class Observation:
    """Record Langfuse observation updates and completion."""

    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []
        self.ended = 0

    def update(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)

    def end(self) -> None:
        self.ended += 1


class LangfuseClient:
    """Record observation creation and flush calls."""

    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []
        self.observations: list[Observation] = []
        self.flushed = 0

    def start_observation(self, **kwargs: Any) -> Observation:
        self.started.append(kwargs)
        observation = Observation()
        self.observations.append(observation)
        return observation

    def flush(self) -> None:
        self.flushed += 1


def test_langfuse_success_error_and_cleanup_paths() -> None:
    client = LangfuseClient()
    adapter = LangfuseTelemetry(client)
    adapter.emit(event(TelemetryEventType.REQUEST_START))
    adapter.emit(event(TelemetryEventType.RETRY, retry_count=1))
    adapter.emit(
        event(
            TelemetryEventType.REQUEST_COMPLETE,
            status=OperationStatus.SUCCEEDED,
            output_payload={"answer": "ok"},
        )
    )
    observation = client.observations[0]
    assert client.started[0]["as_type"] == "generation"
    assert observation.ended == 1
    assert observation.updates[-1]["cost_details"] == {"total": 0.01}
    adapter.emit(event(TelemetryEventType.REQUEST_START, request_id="error"))
    adapter.emit(
        event(
            TelemetryEventType.REQUEST_ERROR,
            request_id="error",
            error={"message": "safe"},
            status=OperationStatus.FAILED,
        )
    )
    assert client.observations[1].updates[-1]["level"] == "ERROR"
    adapter.emit(event(TelemetryEventType.REQUEST_START, request_id="unfinished"))
    adapter.close()
    assert client.observations[2].ended == 1 and client.flushed == 1


class Span:
    """Record OpenTelemetry span attributes, events, and lifecycle."""

    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.status: Any = None
        self.ended = 0

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def add_event(self, name: str, attributes: dict[str, Any]) -> None:
        self.events.append((name, attributes))

    def set_status(self, status: Any) -> None:
        self.status = status

    def end(self) -> None:
        self.ended += 1


class Tracer:
    """Create recording spans."""

    def __init__(self) -> None:
        self.spans: list[Span] = []

    def start_span(self, name: str) -> Span:
        span = Span()
        span.attributes["name"] = name
        self.spans.append(span)
        return span


def test_opentelemetry_complete_error_and_close_paths() -> None:
    tracer = Tracer()
    adapter = OpenTelemetryTelemetry(tracer)
    adapter.emit(event(TelemetryEventType.REQUEST_START))
    adapter.emit(event(TelemetryEventType.RETRY, attributes={"route": {"count": 1}}))
    adapter.emit(event(TelemetryEventType.REQUEST_COMPLETE, status=OperationStatus.SUCCEEDED))
    assert tracer.spans[0].ended == 1
    assert tracer.spans[0].attributes["gen_ai.provider.name"] == "openai"
    assert tracer.spans[0].events[0][0] == "retry"
    adapter.emit(event(TelemetryEventType.REQUEST_START, request_id="error"))
    adapter.emit(
        event(
            TelemetryEventType.REQUEST_ERROR,
            request_id="error",
            error={"message": "failed"},
            status=OperationStatus.FAILED,
        )
    )
    assert tracer.spans[1].ended == 1
    adapter.emit(event(TelemetryEventType.REQUEST_START, request_id="open"))
    adapter.close()
    assert tracer.spans[2].ended == 1


def test_structured_logging_and_litellm_callback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.structured")
    adapter = StructuredLoggingTelemetry(logger)
    with caplog.at_level(logging.INFO, logger="test.structured"):
        adapter.emit(event(TelemetryEventType.REQUEST_START))
    assert "harborrag.model.request_start" in caplog.text
    sink = RecordingSink()
    callback = LiteLLMTelemetryCallback(TelemetryDispatcher((sink,)))
    start = datetime.now(UTC)
    end = start + timedelta(milliseconds=5)
    kwargs = {
        "model": "gpt",
        "response_cost": 0.02,
        "cache_hit": True,
        "litellm_params": {
            "metadata": litellm_telemetry_metadata(
                request_id="r", operation="chat", logical_model="primary"
            )
        },
    }
    callback.log_success_event(kwargs, None, start, end)
    callback.log_failure_event(kwargs, RuntimeError("raw secret"), start, end)
    assert sink.events[0].event_type is TelemetryEventType.PROVIDER_COMPLETE
    assert sink.events[0].latency_ms == 5
    assert sink.events[1].event_type is TelemetryEventType.PROVIDER_ERROR
    assert "raw secret" not in sink.events[1].error["message"]
