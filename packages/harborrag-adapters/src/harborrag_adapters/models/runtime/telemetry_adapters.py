from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from threading import RLock
from typing import Any

from .telemetry import TelemetryEvent, TelemetryEventType
from .telemetry_metrics import OpenTelemetryMetrics


class StructuredLoggingTelemetry:
    """Write telemetry events as structured records through standard logging."""

    def __init__(self, logger: logging.Logger, *, level: int = logging.INFO) -> None:
        """Store the target logger and event level."""

        self.logger = logger
        self.level = level

    def emit(self, event: TelemetryEvent) -> None:
        """Write one structured telemetry record."""

        self.logger.log(
            self.level,
            "harborrag.model.%s",
            event.event_type.value,
            extra={"harborrag_telemetry": event.model_dump(mode="json")},
        )

    async def aemit(self, event: TelemetryEvent) -> None:
        """Write one event without adding asynchronous scheduling overhead."""

        self.emit(event)

    def close(self) -> None:
        """Return because logger lifecycle is application-owned."""

    async def aclose(self) -> None:
        """Return because logger lifecycle is application-owned."""


@dataclass(slots=True)
class LangfuseObservationState:
    """Track one active Langfuse observation and its Harbor policy events."""

    observation: Any
    events: list[dict[str, Any]] = field(default_factory=list)


class LangfuseTelemetry:
    """Map sanitized Harbor events to the optional Langfuse v4 observation API."""

    def __init__(self, client: Any | None = None) -> None:
        """Use an injected Langfuse client or lazily obtain the configured SDK client."""

        if client is None:
            try:
                from langfuse import get_client
            except ImportError as exc:
                raise RuntimeError(
                    "Langfuse telemetry requires harborrag-adapters[langfuse]"
                ) from exc
            client = get_client()
        self.client: Any = client
        self._active: dict[str, LangfuseObservationState] = {}
        self._lock = RLock()

    def emit(self, event: TelemetryEvent) -> None:
        """Create, update, or finish one Langfuse observation."""

        if event.request_id is None:
            return
        with self._lock:
            if event.event_type is TelemetryEventType.REQUEST_START:
                observation_type = _langfuse_type(event.operation)
                arguments: dict[str, Any] = {
                    "name": f"harborrag.{event.operation}",
                    "as_type": observation_type,
                    "input": event.input_payload,
                    "metadata": _langfuse_metadata(event),
                }
                if observation_type in {"generation", "embedding"}:
                    arguments["model"] = event.provider_model or event.logical_model
                observation = self.client.start_observation(**arguments)
                self._active[event.request_id] = LangfuseObservationState(observation)
                return
            state = self._active.get(event.request_id)
            if state is None:
                return
            state.events.append(_event_summary(event))
            if event.event_type is TelemetryEventType.REQUEST_COMPLETE:
                state.observation.update(
                    model=event.provider_model,
                    output=event.output_payload,
                    usage_details=event.usage or None,
                    cost_details=(
                        {"total": event.estimated_cost_usd}
                        if event.estimated_cost_usd is not None
                        else None
                    ),
                    metadata={
                        **_langfuse_metadata(event),
                        "harbor_events": state.events,
                    },
                )
                state.observation.end()
                self._active.pop(event.request_id, None)
            elif event.event_type is TelemetryEventType.REQUEST_ERROR:
                state.observation.update(
                    level="ERROR",
                    status_message=(event.error or {}).get("message"),
                    metadata={
                        **_langfuse_metadata(event),
                        "harbor_events": state.events,
                    },
                )
                state.observation.end()
                self._active.pop(event.request_id, None)

    async def aemit(self, event: TelemetryEvent) -> None:
        """Map an event without blocking on network export, which Langfuse batches."""

        self.emit(event)

    def close(self) -> None:
        """End unfinished observations and flush the Langfuse client."""

        with self._lock:
            for state in self._active.values():
                state.observation.update(level="WARNING", status_message="client closed")
                state.observation.end()
            self._active.clear()
        flush = getattr(self.client, "flush", None)
        if callable(flush):
            flush()

    async def aclose(self) -> None:
        """Flush blocking Langfuse clients without delaying the event loop."""

        if callable(getattr(self.client, "flush", None)):
            await asyncio.to_thread(self.close)
        else:
            self.close()


class OpenTelemetryTelemetry:
    """Map sanitized Harbor events onto OpenTelemetry spans and span events."""

    def __init__(self, tracer: Any | None = None, meter: Any | None = None) -> None:
        """Use injected OpenTelemetry APIs or lazily resolve application providers."""

        if tracer is None or meter is None:
            try:
                from opentelemetry import trace
            except ImportError as exc:
                raise RuntimeError(
                    "OpenTelemetry telemetry requires harborrag-adapters[opentelemetry]"
                ) from exc
            tracer = tracer or trace.get_tracer("harborrag.models")
            if meter is None:
                from opentelemetry import metrics

                meter = metrics.get_meter("harborrag.models")
        self.tracer = tracer
        self.metrics = OpenTelemetryMetrics(meter)
        self._active: dict[str, Any] = {}
        self._lock = RLock()

    def emit(self, event: TelemetryEvent) -> None:
        """Create spans and record low-cardinality metrics from one sanitized event."""

        self.metrics.record(event)
        if event.request_id is None:
            return
        with self._lock:
            if event.event_type is TelemetryEventType.REQUEST_START:
                span = self.tracer.start_span(f"harborrag.model.{event.operation}")
                self._active[event.request_id] = span
                _set_span_attributes(span, event)
                return
            span = self._active.get(event.request_id)
            if span is None:
                return
            span.add_event(event.event_type.value, _otel_attributes(event))
            _set_span_attributes(span, event)
            if event.event_type is TelemetryEventType.REQUEST_ERROR:
                _set_error_status(span, event)
                span.end()
                self._active.pop(event.request_id, None)
            elif event.event_type is TelemetryEventType.REQUEST_COMPLETE:
                span.end()
                self._active.pop(event.request_id, None)

    async def aemit(self, event: TelemetryEvent) -> None:
        """Map one event through the synchronous, non-exporting span API."""

        self.emit(event)

    def close(self) -> None:
        """End any spans left open by cancelled operations."""

        with self._lock:
            for span in self._active.values():
                span.end()
            self._active.clear()

    async def aclose(self) -> None:
        """End any unfinished spans without owning the tracer provider."""

        self.close()


def _langfuse_type(operation: str) -> str:
    return {"chat": "generation", "embed": "embedding"}.get(operation, "span")


def _langfuse_metadata(event: TelemetryEvent) -> dict[str, Any]:
    return {
        **event.metadata,
        "logical_model": event.logical_model,
        "model_alias": event.model_alias,
        "provider": event.provider,
        "deployment": event.deployment,
        "streaming": event.streaming,
    }


def _event_summary(event: TelemetryEvent) -> dict[str, Any]:
    return {
        "type": event.event_type.value,
        "retry_count": event.retry_count,
        "fallback_count": event.fallback_count,
        "cache_status": event.cache_status,
        "attributes": event.attributes,
        "error": event.error,
    }


def _otel_attributes(event: TelemetryEvent) -> dict[str, Any]:
    raw = {
        "harborrag.request.id": event.request_id,
        "harborrag.trace.id": event.trace_id,
        "harborrag.workflow.id": event.workflow_id,
        "harborrag.tenant.id": event.tenant_id,
        "harborrag.user.id": event.user_id,
        "gen_ai.operation.name": event.operation,
        "gen_ai.request.model": event.logical_model,
        "gen_ai.response.model": event.provider_model,
        "gen_ai.provider.name": event.provider,
        "harborrag.deployment": event.deployment,
        "harborrag.retry.count": event.retry_count,
        "harborrag.fallback.count": event.fallback_count,
        "harborrag.cache.status": event.cache_status,
        "harborrag.streaming": event.streaming,
        "harborrag.latency_ms": event.latency_ms,
        "harborrag.total_duration_ms": event.total_duration_ms,
        "harborrag.first_token_latency_ms": event.first_token_latency_ms,
        "harborrag.error": event.error,
    }
    return {key: _otel_value(value) for key, value in raw.items() if value is not None}


def _set_span_attributes(span: Any, event: TelemetryEvent) -> None:
    for key, value in _otel_attributes(event).items():
        span.set_attribute(key, value)


def _set_error_status(span: Any, event: TelemetryEvent) -> None:
    try:
        from opentelemetry.trace import Status, StatusCode

        span.set_status(Status(StatusCode.ERROR, (event.error or {}).get("message")))
    except ImportError:
        return


def _otel_value(value: Any) -> Any:
    if isinstance(value, str | bool | int | float):
        return value
    return json.dumps(value, sort_keys=True, default=str)
