from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from harborrag_adapters.models.common.litellm_telemetry import (
    LiteLLMTelemetryCallback,
)
from harborrag_adapters.models.common.telemetry import (
    OperationStatus,
    TelemetryDispatcher,
    TelemetryEvent,
    TelemetryEventType,
)
from harborrag_adapters.models.common.telemetry_adapters import (
    LangfuseTelemetry,
    OpenTelemetryTelemetry,
    StructuredLoggingTelemetry,
)

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


class RecordingSink:
    """Record callback bridge events."""

    def __init__(self) -> None:
        self.events: list[TelemetryEvent] = []

    def emit(self, event: TelemetryEvent) -> None:
        """Append one provider callback event."""

        self.events.append(event)

    async def aemit(self, event: TelemetryEvent) -> None:
        """Append one asynchronous provider callback event."""

        self.events.append(event)


class FakeObservation:
    """Capture Langfuse observation mutations."""

    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []
        self.ended = False

    def update(self, **kwargs: Any) -> None:
        """Record an observation update."""

        self.updates.append(kwargs)

    def end(self) -> None:
        """Record observation completion."""

        self.ended = True


class FakeLangfuse:
    """Capture calls made against the Langfuse v4 client boundary."""

    def __init__(self) -> None:
        self.starts: list[dict[str, Any]] = []
        self.observation = FakeObservation()
        self.flushed = False

    def start_observation(self, **kwargs: Any) -> FakeObservation:
        """Return the fake active observation."""

        self.starts.append(kwargs)
        return self.observation

    def flush(self) -> None:
        """Record export flushing."""

        self.flushed = True


class FakeSpan:
    """Capture OpenTelemetry span attributes and events."""

    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.status: Any = None
        self.ended = False

    def set_attribute(self, key: str, value: Any) -> None:
        """Record one span attribute."""

        self.attributes[key] = value

    def add_event(self, name: str, attributes: dict[str, Any]) -> None:
        """Record one span event."""

        self.events.append((name, attributes))

    def set_status(self, status: Any) -> None:
        """Record the span status object."""

        self.status = status

    def end(self) -> None:
        """Record span completion."""

        self.ended = True


class FakeTracer:
    """Create and retain one fake OpenTelemetry span."""

    def __init__(self) -> None:
        self.names: list[str] = []
        self.span = FakeSpan()

    def start_span(self, name: str) -> FakeSpan:
        """Return a span for a request start."""

        self.names.append(name)
        return self.span


def _event(
    event_type: TelemetryEventType,
    *,
    status: OperationStatus = OperationStatus.IN_PROGRESS,
    **updates: Any,
) -> TelemetryEvent:
    return TelemetryEvent(
        event_type=event_type,
        operation="chat",
        status=status,
        request_id="request-1",
        logical_model="primary",
        model_alias="friendly",
        provider="openai",
        provider_model="gpt-test",
        deployment="primary-0",
        **updates,
    )


def test_structured_logging_adapter_writes_event_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("harborrag.telemetry.test")
    adapter = StructuredLoggingTelemetry(logger)

    with caplog.at_level(logging.INFO, logger=logger.name):
        adapter.emit(_event(TelemetryEventType.CACHE_HIT, cache_status="hit"))

    record = caplog.records[-1]
    assert record.message == "harborrag.model.cache_hit"
    assert record.harborrag_telemetry["request_id"] == "request-1"
    assert record.harborrag_telemetry["cache_status"] == "hit"


def test_langfuse_adapter_maps_lifecycle_and_flushes() -> None:
    client = FakeLangfuse()
    adapter = LangfuseTelemetry(client)
    adapter.emit(
        _event(
            TelemetryEventType.REQUEST_START,
            status=OperationStatus.STARTED,
            input_payload={"messages": "allowed"},
        )
    )
    adapter.emit(_event(TelemetryEventType.RETRY, retry_count=1))
    adapter.emit(
        _event(
            TelemetryEventType.REQUEST_COMPLETE,
            status=OperationStatus.SUCCEEDED,
            usage={"total_tokens": 3},
            estimated_cost_usd=0.01,
            output_payload={"text": "allowed"},
        )
    )
    adapter.close()

    assert client.starts[0]["as_type"] == "generation"
    assert client.starts[0]["model"] == "gpt-test"
    assert client.observation.ended
    update = client.observation.updates[-1]
    assert update["usage_details"] == {"total_tokens": 3}
    assert update["cost_details"] == {"total": 0.01}
    assert update["metadata"]["harbor_events"][0]["type"] == "retry"
    assert client.flushed


def test_langfuse_adapter_uses_span_for_reranking_without_model_parameter() -> None:
    client = FakeLangfuse()
    adapter = LangfuseTelemetry(client)
    adapter.emit(
        TelemetryEvent(
            event_type=TelemetryEventType.REQUEST_START,
            operation="rerank",
            status=OperationStatus.STARTED,
            request_id="rerank-1",
            logical_model="primary",
        )
    )

    assert client.starts[0]["as_type"] == "span"
    assert "model" not in client.starts[0]


def test_opentelemetry_adapter_maps_attributes_events_and_error_status() -> None:
    tracer = FakeTracer()
    adapter = OpenTelemetryTelemetry(tracer)
    adapter.emit(_event(TelemetryEventType.REQUEST_START, status=OperationStatus.STARTED))
    adapter.emit(
        _event(
            TelemetryEventType.CACHE_MISS,
            cache_status="miss",
            tenant_id="hashed-tenant",
        )
    )
    adapter.emit(
        _event(
            TelemetryEventType.REQUEST_ERROR,
            status=OperationStatus.FAILED,
            error={"type": "Timeout", "message": "safe"},
        )
    )

    assert tracer.names == ["harborrag.model.chat"]
    assert tracer.span.attributes["gen_ai.request.model"] == "primary"
    assert tracer.span.attributes["harborrag.cache.status"] == "miss"
    assert [name for name, _ in tracer.span.events] == ["cache_miss", "request_error"]
    assert tracer.span.ended


def test_litellm_callback_emits_correlated_success_and_sanitized_failure() -> None:
    sink = RecordingSink()
    callback = LiteLLMTelemetryCallback(TelemetryDispatcher([sink]))
    started = datetime.now(UTC)
    finished = started + timedelta(milliseconds=25)
    kwargs = {
        "model": "openai/gpt-test",
        "response_cost": 0.02,
        "cache_hit": True,
        "litellm_params": {
            "metadata": {
                "harborrag": {
                    "request_id": "request-1",
                    "operation": "chat",
                    "logical_model": "primary",
                }
            }
        },
    }

    callback.log_success_event(kwargs, {}, started, finished)
    callback.log_failure_event(kwargs, RuntimeError("api_key=top-secret"), started, finished)

    success, failure = sink.events
    assert success.event_type is TelemetryEventType.PROVIDER_COMPLETE
    assert success.request_id == "request-1"
    assert success.latency_ms == pytest.approx(25)
    assert success.estimated_cost_usd == 0.02
    assert success.cache_status == "hit"
    assert failure.event_type is TelemetryEventType.PROVIDER_ERROR
    assert "top-secret" not in str(failure.error)
