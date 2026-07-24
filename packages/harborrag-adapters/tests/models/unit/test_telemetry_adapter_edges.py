from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from harborrag_adapters.models.runtime.litellm_telemetry import LiteLLMTelemetryCallback
from harborrag_adapters.models.runtime.telemetry import (
    OperationStatus,
    TelemetryDispatcher,
    TelemetryEvent,
    TelemetryEventType,
)
from harborrag_adapters.models.runtime.telemetry_adapters import (
    LangfuseTelemetry,
    OpenTelemetryTelemetry,
    StructuredLoggingTelemetry,
)

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


class Observation:
    """Record observation updates and closure."""

    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []
        self.ended = 0

    def update(self, **kwargs: Any) -> None:
        """Record one update."""

        self.updates.append(kwargs)

    def end(self) -> None:
        """Record observation completion."""

        self.ended += 1


class LangfuseClient:
    """Create observations without implementing optional flush."""

    def __init__(self) -> None:
        self.observations: list[Observation] = []

    def start_observation(self, **_kwargs: Any) -> Observation:
        """Create and retain an observation."""

        observation = Observation()
        self.observations.append(observation)
        return observation


class Span:
    """Record span mutations."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.attributes: dict[str, Any] = {}
        self.ended = 0

    def add_event(self, name: str, _attributes: dict[str, Any]) -> None:
        """Record one span event name."""

        self.events.append(name)

    def set_attribute(self, key: str, value: Any) -> None:
        """Record a span attribute."""

        self.attributes[key] = value

    def set_status(self, _status: Any) -> None:
        """Accept an error status."""

    def end(self) -> None:
        """Record span completion."""

        self.ended += 1


class Tracer:
    """Create a new span for each request."""

    def __init__(self) -> None:
        self.spans: list[Span] = []

    def start_span(self, _name: str) -> Span:
        """Create and retain one span."""

        span = Span()
        self.spans.append(span)
        return span


class Sink:
    """Record provider callback events."""

    def __init__(self) -> None:
        self.events: list[TelemetryEvent] = []

    def emit(self, event: TelemetryEvent) -> None:
        """Record a synchronous event."""

        self.events.append(event)

    async def aemit(self, event: TelemetryEvent) -> None:
        """Record an asynchronous event."""

        self.events.append(event)


def _event(event_type: TelemetryEventType, request_id: str | None = "request") -> TelemetryEvent:
    return TelemetryEvent(
        event_type=event_type,
        operation="embed",
        status=OperationStatus.IN_PROGRESS,
        request_id=request_id,
        logical_model="primary",
        error=({"message": "safe"} if event_type is TelemetryEventType.REQUEST_ERROR else None),
    )


@pytest.mark.asyncio
async def test_structured_logging_async_lifecycle(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("harborrag.telemetry.async")
    adapter = StructuredLoggingTelemetry(logger)
    with caplog.at_level(logging.INFO, logger=logger.name):
        await adapter.aemit(_event(TelemetryEventType.CACHE_BYPASS))
    assert caplog.records[-1].message == "harborrag.model.cache_bypass"
    assert adapter.close() is None
    assert await adapter.aclose() is None


@pytest.mark.asyncio
async def test_langfuse_error_unknown_and_async_close_paths() -> None:
    client = LangfuseClient()
    adapter = LangfuseTelemetry(client)
    adapter.emit(_event(TelemetryEventType.REQUEST_START, request_id=None))
    adapter.emit(_event(TelemetryEventType.RETRY, request_id="unknown"))

    await adapter.aemit(_event(TelemetryEventType.REQUEST_START, request_id="error"))
    await adapter.aemit(_event(TelemetryEventType.REQUEST_ERROR, request_id="error"))
    assert client.observations[0].updates[-1]["level"] == "ERROR"
    assert client.observations[0].ended == 1

    adapter.emit(_event(TelemetryEventType.REQUEST_START, request_id="cancelled"))
    await adapter.aclose()
    assert client.observations[1].updates[-1]["level"] == "WARNING"
    assert client.observations[1].ended == 1


@pytest.mark.asyncio
async def test_opentelemetry_completion_unknown_and_close_paths() -> None:
    tracer = Tracer()
    adapter = OpenTelemetryTelemetry(tracer)
    adapter.emit(_event(TelemetryEventType.REQUEST_START, request_id=None))
    adapter.emit(_event(TelemetryEventType.CACHE_MISS, request_id="unknown"))

    await adapter.aemit(_event(TelemetryEventType.REQUEST_START, request_id="complete"))
    await adapter.aemit(_event(TelemetryEventType.REQUEST_COMPLETE, request_id="complete"))
    assert tracer.spans[0].events == ["request_complete"]
    assert tracer.spans[0].ended == 1

    adapter.emit(_event(TelemetryEventType.REQUEST_START, request_id="sync-close"))
    adapter.close()
    assert tracer.spans[1].ended == 1
    adapter.emit(_event(TelemetryEventType.REQUEST_START, request_id="async-close"))
    await adapter.aclose()
    assert tracer.spans[2].ended == 1


@pytest.mark.asyncio
async def test_litellm_async_callbacks_handle_fallback_error_and_invalid_cost() -> None:
    sink = Sink()
    callback = LiteLLMTelemetryCallback(TelemetryDispatcher([sink]))
    start = datetime.now(UTC)
    end = start + timedelta(milliseconds=1)
    kwargs = {
        "model": "provider/model",
        "response_cost": -1,
        "litellm_params": {"metadata": "invalid"},
    }

    await callback.async_log_success_event(kwargs, {}, start, end)
    await callback.async_log_failure_event(kwargs, {"error": "not-exception"}, start, end)

    success, failure = sink.events
    assert success.operation == "model"
    assert success.estimated_cost_usd is None
    assert success.cache_status == "miss"
    assert failure.error == {
        "type": "RuntimeError",
        "message": "RuntimeError: provider request failed",
    }
