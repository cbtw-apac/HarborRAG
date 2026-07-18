from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from harborrag_core.models.errors import HarborModelError
from pydantic import BaseModel

from .cache import CacheDecision
from .errors import safe_provider_error_message
from .telemetry import (
    OperationStatus,
    TelemetryDispatcher,
    TelemetryEvent,
    TelemetryEventType,
)


class ModelTelemetryOperation:
    """Build sanitized lifecycle events for one sync or async model request."""

    def __init__(
        self,
        dispatcher: TelemetryDispatcher,
        *,
        operation: str,
        request: BaseModel,
        model_alias: str,
        logical_model: str,
    ) -> None:
        """Capture stable request context without retaining unsanitized payload copies."""

        self.dispatcher = dispatcher
        self.operation = operation
        self.request = request
        self.model_alias = model_alias
        self.logical_model = logical_model
        self.started = time.perf_counter()
        self.first_token_latency_ms: float | None = None
        raw_metadata = _model_mapping(getattr(request, "metadata", {}))
        self.metadata = raw_metadata
        self.request_id = _string_value(raw_metadata.get("request_id"))
        self.trace_id = _string_value(raw_metadata.get("trace_id"))
        self.workflow_id = _string_value(raw_metadata.get("workflow_id"))
        self.tenant_id = _string_value(raw_metadata.get("tenant_id"))
        self.user_id = _string_value(raw_metadata.get("user_id"))

    def start(self, *, streaming: bool = False) -> None:
        """Emit request and optional stream-start events synchronously."""

        self.dispatcher.emit(self._start_event(streaming=streaming))
        if streaming:
            self.dispatcher.emit(
                self._event(
                    TelemetryEventType.STREAM_START,
                    OperationStatus.STARTED,
                    streaming=True,
                )
            )

    async def astart(self, *, streaming: bool = False) -> None:
        """Emit request and optional stream-start events asynchronously."""

        await self.dispatcher.aemit(self._start_event(streaming=streaming))
        if streaming:
            await self.dispatcher.aemit(
                self._event(
                    TelemetryEventType.STREAM_START,
                    OperationStatus.STARTED,
                    streaming=True,
                )
            )

    def cache(self, decision: CacheDecision, *, hit: bool) -> None:
        """Emit a cache hit, miss, or policy-bypass event synchronously."""

        self.dispatcher.emit(self._cache_event(decision, hit=hit))

    async def acache(self, decision: CacheDecision, *, hit: bool) -> None:
        """Emit a cache hit, miss, or policy-bypass event asynchronously."""

        await self.dispatcher.aemit(self._cache_event(decision, hit=hit))

    def transition(self, transition: Any) -> None:
        """Emit one sanitized retry or fallback transition synchronously."""

        self.dispatcher.emit(self._transition_event(transition))

    async def atransition(self, transition: Any) -> None:
        """Emit one sanitized retry or fallback transition asynchronously."""

        await self.dispatcher.aemit(self._transition_event(transition))

    def complete(self, response: BaseModel, *, streaming: bool = False) -> None:
        """Emit successful completion events synchronously."""

        if streaming:
            self.dispatcher.emit(self._stream_complete_event(response))
        self.dispatcher.emit(self._completion_event(response, streaming=streaming))

    async def acomplete(self, response: BaseModel, *, streaming: bool = False) -> None:
        """Emit successful completion events asynchronously."""

        if streaming:
            await self.dispatcher.aemit(self._stream_complete_event(response))
        await self.dispatcher.aemit(self._completion_event(response, streaming=streaming))

    def error(self, error: Exception, *, streaming: bool = False) -> None:
        """Emit sanitized operation failure events synchronously."""

        if streaming:
            self.dispatcher.emit(self._error_event(error, stream=True))
        self.dispatcher.emit(self._error_event(error, stream=False))

    async def aerror(self, error: Exception, *, streaming: bool = False) -> None:
        """Emit sanitized operation failure events asynchronously."""

        if streaming:
            await self.dispatcher.aemit(self._error_event(error, stream=True))
        await self.dispatcher.aemit(self._error_event(error, stream=False))

    def stream_event(self, chunk: BaseModel) -> None:
        """Emit one normalized streaming event and track first-token latency."""

        event = self._stream_event(chunk)
        if event is not None:
            self.dispatcher.emit(event)

    async def astream_event(self, chunk: BaseModel) -> None:
        """Emit one normalized streaming event asynchronously."""

        event = self._stream_event(chunk)
        if event is not None:
            await self.dispatcher.aemit(event)

    def _start_event(self, *, streaming: bool) -> TelemetryEvent:
        return self._event(
            TelemetryEventType.REQUEST_START,
            OperationStatus.STARTED,
            streaming=streaming,
            input_payload=self.request,
        )

    def _cache_event(self, decision: CacheDecision, *, hit: bool) -> TelemetryEvent:
        event_type = (
            TelemetryEventType.CACHE_HIT
            if hit
            else (
                TelemetryEventType.CACHE_MISS
                if decision.allowed
                else TelemetryEventType.CACHE_BYPASS
            )
        )
        return self._event(
            event_type,
            OperationStatus.IN_PROGRESS,
            cache_status=event_type.value.removeprefix("cache_"),
            attributes={"reason": decision.reason},
        )

    def _transition_event(self, transition: Any) -> TelemetryEvent:
        kind = str(transition.kind)
        event_type = {
            "retry": TelemetryEventType.RETRY,
            "deployment_fallback": TelemetryEventType.DEPLOYMENT_FALLBACK,
            "model_fallback": TelemetryEventType.MODEL_FALLBACK,
        }[kind]
        attempt = transition.attempt
        deployment = attempt.deployment
        return self._event(
            event_type,
            OperationStatus.IN_PROGRESS,
            logical_model=attempt.logical_model,
            provider=_enum_value(getattr(deployment, "provider", None)),
            provider_model=getattr(deployment, "model", None),
            deployment=deployment.name,
            retry_count=transition.retry_count,
            fallback_count=(transition.deployment_failover_count + transition.model_fallback_count),
            error=_safe_error(transition.error, self.dispatcher),
        )

    def _completion_event(self, response: BaseModel, *, streaming: bool) -> TelemetryEvent:
        usage = _model_mapping(getattr(response, "usage", {}))
        return self._event(
            TelemetryEventType.REQUEST_COMPLETE,
            OperationStatus.SUCCEEDED,
            streaming=streaming,
            provider=getattr(response, "provider", None),
            provider_model=getattr(response, "provider_model", None),
            deployment=getattr(response, "deployment", None),
            latency_ms=getattr(response, "latency_ms", None),
            first_token_latency_ms=(
                self.first_token_latency_ms or getattr(response, "first_token_latency_ms", None)
            ),
            total_duration_ms=self._elapsed(),
            usage=usage,
            estimated_cost_usd=getattr(response, "estimated_cost_usd", None),
            retry_count=getattr(response, "retry_count", 0),
            fallback_count=getattr(response, "fallback_count", 0),
            cache_status="hit" if getattr(response, "cache_hit", False) else "miss",
            tool_calls=_tool_calls(
                response,
                include_arguments=True,
            ),
            output_payload=response,
        )

    def _stream_event(self, chunk: BaseModel) -> TelemetryEvent | None:
        has_token = bool(
            getattr(chunk, "text_delta", None)
            or getattr(chunk, "reasoning_delta", None)
            or getattr(chunk, "tool_call_delta", None)
        )
        if has_token and self.first_token_latency_ms is None:
            self.first_token_latency_ms = self._elapsed()
        if not self.dispatcher.include_stream_events:
            return None
        return self._event(
            TelemetryEventType.STREAM_EVENT,
            OperationStatus.IN_PROGRESS,
            streaming=True,
            first_token_latency_ms=self.first_token_latency_ms,
            provider=getattr(chunk, "provider", None),
            provider_model=getattr(chunk, "provider_model", None),
            deployment=getattr(chunk, "deployment", None),
            tool_calls=_tool_calls(
                chunk,
                include_arguments=True,
            ),
            output_payload=chunk,
            attributes={"stream_event": _enum_value(getattr(chunk, "event", None))},
        )

    def _stream_complete_event(self, response: BaseModel) -> TelemetryEvent:
        return self._event(
            TelemetryEventType.STREAM_COMPLETE,
            OperationStatus.SUCCEEDED,
            streaming=True,
            first_token_latency_ms=self.first_token_latency_ms,
            total_duration_ms=self._elapsed(),
            provider=getattr(response, "provider", None),
            provider_model=getattr(response, "provider_model", None),
            deployment=getattr(response, "deployment", None),
            usage=_model_mapping(getattr(response, "usage", {})),
            tool_calls=_tool_calls(
                response,
                include_arguments=True,
            ),
        )

    def _error_event(self, error: Exception, *, stream: bool) -> TelemetryEvent:
        return self._event(
            (TelemetryEventType.STREAM_ERROR if stream else TelemetryEventType.REQUEST_ERROR),
            OperationStatus.FAILED,
            streaming=stream,
            total_duration_ms=self._elapsed(),
            error=_safe_error(error, self.dispatcher),
            logical_model=getattr(error, "logical_model", None) or self.logical_model,
            provider=getattr(error, "provider", None),
            provider_model=getattr(error, "provider_model", None),
            deployment=getattr(error, "deployment", None),
        )

    def _event(
        self,
        event_type: TelemetryEventType,
        status: OperationStatus,
        **updates: Any,
    ) -> TelemetryEvent:
        base = {
            "event_type": event_type,
            "operation": self.operation,
            "status": status,
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "workflow_id": self.workflow_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "model_alias": self.model_alias,
            "logical_model": self.logical_model,
            "metadata": self.metadata,
        }
        return TelemetryEvent(**{**base, **updates})

    def _elapsed(self) -> float:
        return (time.perf_counter() - self.started) * 1_000


def _model_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _safe_error(error: Exception, dispatcher: TelemetryDispatcher) -> dict[str, Any]:
    value = (
        error.to_dict()
        if isinstance(error, HarborModelError)
        else {
            "type": type(error).__name__,
            "message": safe_provider_error_message(error),
        }
    )
    sanitized = dispatcher.privacy.sanitize(value)
    return sanitized if isinstance(sanitized, dict) else {"type": type(error).__name__}


def _tool_calls(value: Any, *, include_arguments: bool) -> tuple[dict[str, Any], ...]:
    calls = list(getattr(value, "tool_calls", ()) or ())
    delta = getattr(value, "tool_call_delta", None)
    if delta is not None:
        calls.append(delta)
    result: list[dict[str, Any]] = []
    for call in calls:
        function = getattr(call, "function", None)
        item = {
            "id": getattr(call, "id", None),
            "type": getattr(call, "type", None),
            "name": getattr(function, "name", None),
        }
        if include_arguments:
            item["arguments"] = getattr(function, "parsed_arguments", None) or getattr(
                function, "arguments", None
            )
        result.append(item)
    return tuple(result)


def _enum_value(value: Any) -> str | None:
    raw = getattr(value, "value", value)
    return str(raw) if raw is not None else None


def _string_value(value: Any) -> str | None:
    return str(value) if value is not None else None
