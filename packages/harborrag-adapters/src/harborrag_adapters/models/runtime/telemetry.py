from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .config import ObservabilityConfig, TelemetryFailureMode
from .security import PrivacyConfig, PrivacySanitizer
from .sync import run_awaitable_synchronously


class TelemetryEventType(StrEnum):
    """Enumerate provider-neutral model lifecycle and policy events."""

    REQUEST_START = "request_start"
    REQUEST_COMPLETE = "request_complete"
    REQUEST_ERROR = "request_error"
    STREAM_START = "stream_start"
    STREAM_EVENT = "stream_event"
    STREAM_COMPLETE = "stream_complete"
    STREAM_ERROR = "stream_error"
    RETRY = "retry"
    DEPLOYMENT_FALLBACK = "deployment_fallback"
    MODEL_FALLBACK = "model_fallback"
    CACHE_HIT = "cache_hit"
    CACHE_MISS = "cache_miss"
    CACHE_BYPASS = "cache_bypass"
    PROVIDER_COMPLETE = "provider_complete"
    PROVIDER_ERROR = "provider_error"


class OperationStatus(StrEnum):
    """Describe the state of a model operation at one telemetry event."""

    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    IN_PROGRESS = "in_progress"


class TelemetryEvent(BaseModel):
    """Carry one sanitized adapter-level telemetry event to optional sinks."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: TelemetryEventType
    operation: str
    status: OperationStatus
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    request_id: str | None = None
    trace_id: str | None = None
    workflow_id: str | None = None
    tenant_id: str | None = None
    user_id: str | None = None
    model_alias: str | None = None
    logical_model: str | None = None
    provider: str | None = None
    provider_model: str | None = None
    deployment: str | None = None
    latency_ms: float | None = Field(default=None, ge=0)
    first_token_latency_ms: float | None = Field(default=None, ge=0)
    total_duration_ms: float | None = Field(default=None, ge=0)
    usage: dict[str, Any] = Field(default_factory=dict)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    retry_count: int = Field(default=0, ge=0)
    fallback_count: int = Field(default=0, ge=0)
    cache_status: str | None = None
    streaming: bool = False
    tool_calls: tuple[dict[str, Any], ...] = ()
    input_payload: Any = None
    output_payload: Any = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class TelemetrySink(Protocol):
    """Define the sync/async event and lifecycle boundary for telemetry adapters."""

    def emit(self, event: TelemetryEvent) -> None:
        """Handle one event synchronously."""
        ...

    async def aemit(self, event: TelemetryEvent) -> None:
        """Handle one event asynchronously."""
        ...

    def close(self) -> None:
        """Release synchronous resources."""
        ...

    async def aclose(self) -> None:
        """Release asynchronous resources."""
        ...


class TelemetryDispatchError(RuntimeError):
    """Report a sink failure when strict telemetry mode is enabled."""


class TelemetryHookLifecycle(Protocol):
    """Retain the minimal lifecycle shape accepted by existing integrations."""

    async def aclose(self) -> None:
        """Release resources held by the telemetry adapter."""
        ...


class TelemetryDispatcher:
    """Dispatch sanitized events with configurable backend-failure isolation."""

    def __init__(
        self,
        sinks: Sequence[object],
        *,
        config: ObservabilityConfig | None = None,
        failure_mode: TelemetryFailureMode | None = None,
        logger: logging.Logger | None = None,
        enabled: bool | None = None,
    ) -> None:
        """Store sinks, privacy policy, and strict-versus-isolated failure behavior."""

        resolved = config or ObservabilityConfig()
        self._sinks = tuple(sinks) if (resolved.enabled if enabled is None else enabled) else ()
        self.failure_mode = failure_mode or resolved.failure_mode
        self.logger = logger or logging.getLogger("harborrag.models.telemetry")
        self.privacy = PrivacySanitizer(resolved.privacy)
        self.include_stream_events = resolved.include_stream_events

    @property
    def enabled(self) -> bool:
        """Return whether at least one telemetry sink is active."""

        return bool(self._sinks)

    def emit(self, event: TelemetryEvent) -> None:
        """Send one event to every sink in registration order."""

        if not self._sinks:
            return
        prepared = self._prepare_safely(event)
        if prepared is None:
            return
        for sink in self._sinks:
            callback = getattr(sink, "emit", None)
            if callback is None:
                continue
            try:
                callback(prepared)
            except Exception as exc:
                self._handle_failure(exc, sink, "emit")

    async def aemit(self, event: TelemetryEvent) -> None:
        """Send one event asynchronously, accepting synchronous sink fallbacks."""

        if not self._sinks:
            return
        prepared = self._prepare_safely(event)
        if prepared is None:
            return
        for sink in self._sinks:
            try:
                callback = getattr(sink, "aemit", None)
                if callback is not None:
                    await callback(prepared)
                elif (sync_callback := getattr(sink, "emit", None)) is not None:
                    sync_callback(prepared)
            except Exception as exc:
                self._handle_failure(exc, sink, "aemit")

    def close(self) -> None:
        """Close every sink and apply telemetry failure policy after cleanup."""

        errors: list[Exception] = []
        for sink in self._sinks:
            try:
                if (callback := getattr(sink, "close", None)) is not None:
                    callback()
                elif (async_callback := getattr(sink, "aclose", None)) is not None:
                    run_awaitable_synchronously(
                        async_callback(), thread_name="harbor-telemetry-close"
                    )
            except Exception as exc:
                errors.append(exc)
                self._log_failure(exc, sink, "close")
        self._raise_close_errors(errors)

    async def aclose(self) -> None:
        """Asynchronously close every sink before applying telemetry failure policy."""

        errors: list[Exception] = []
        for sink in self._sinks:
            try:
                if (callback := getattr(sink, "aclose", None)) is not None:
                    await callback()
                elif (sync_callback := getattr(sink, "close", None)) is not None:
                    sync_callback()
            except Exception as exc:
                errors.append(exc)
                self._log_failure(exc, sink, "aclose")
        self._raise_close_errors(errors)

    def _handle_failure(self, exc: Exception, sink: object, method: str) -> None:
        if self.failure_mode is TelemetryFailureMode.RAISE:
            raise TelemetryDispatchError(
                f"telemetry sink {type(sink).__name__}.{method} failed"
            ) from exc
        self._log_failure(exc, sink, method)

    def _prepare(self, event: TelemetryEvent) -> TelemetryEvent:
        error = self.privacy.sanitize(event.error) if event.error is not None else None
        usage = self.privacy.sanitize(event.usage)
        attributes = self.privacy.sanitize(event.attributes)
        calls = self.privacy.sanitize(event.tool_calls)
        tool_calls = tuple(item for item in calls if isinstance(item, dict))
        if not self.privacy.config.log_outputs:
            tool_calls = tuple(
                {key: value for key, value in call.items() if key != "arguments"}
                for call in tool_calls
            )
        return event.model_copy(
            update={
                "tenant_id": self.privacy.identifier(event.tenant_id),
                "user_id": self.privacy.identifier(event.user_id),
                "metadata": self.privacy.metadata(event.metadata),
                "input_payload": (
                    self.privacy.content(event.input_payload)
                    if self.privacy.config.log_inputs
                    else None
                ),
                "output_payload": (
                    self.privacy.content(event.output_payload)
                    if self.privacy.config.log_outputs
                    else None
                ),
                "tool_calls": tool_calls,
                "usage": usage if isinstance(usage, dict) else {},
                "error": error if isinstance(error, dict) else None,
                "attributes": attributes if isinstance(attributes, dict) else {},
            }
        )

    def _prepare_safely(self, event: TelemetryEvent) -> TelemetryEvent | None:
        try:
            return self._prepare(event)
        except Exception as exc:
            self._handle_failure(exc, self, "prepare")
            return None

    def _log_failure(self, exc: Exception, sink: object, method: str) -> None:
        self.logger.error(
            "Telemetry sink %s.%s failed: %s",
            type(sink).__name__,
            method,
            type(exc).__name__,
        )

    def _raise_close_errors(self, errors: list[Exception]) -> None:
        if not errors or self.failure_mode is not TelemetryFailureMode.RAISE:
            return
        if len(errors) == 1:
            raise errors[0]
        raise ExceptionGroup("errors while closing telemetry sinks", errors)


def disabled_telemetry(privacy: PrivacyConfig | None = None) -> TelemetryDispatcher:
    """Build a no-op dispatcher that retains the configured privacy policy."""

    return TelemetryDispatcher(
        (),
        config=ObservabilityConfig(enabled=False, privacy=privacy or PrivacyConfig()),
    )


def litellm_telemetry_metadata(
    *,
    request_id: str | None,
    operation: str,
    logical_model: str,
    request_metadata: object | None = None,
    privacy: PrivacyConfig | None = None,
) -> dict[str, Any]:
    """Build privacy-enforced LiteLLM callback and Proxy spend metadata."""

    policy = privacy or PrivacyConfig()
    raw = (
        request_metadata.model_dump(mode="python", exclude_none=True)
        if isinstance(request_metadata, BaseModel)
        else {}
    )
    sanitized = PrivacySanitizer(policy).metadata(raw)
    harbor = {
        **sanitized,
        "request_id": request_id,
        "operation": operation,
        "logical_model": logical_model,
    }
    result: dict[str, Any] = {"harborrag": harbor}
    if policy.propagate_proxy_metadata:
        result["trace_id"] = sanitized.get("trace_id")
        result["session_id"] = sanitized.get("conversation_id")
        if policy.propagate_user_identifiers:
            result["user"] = sanitized.get("user_id")
            result["tenant_id"] = sanitized.get("tenant_id")
    return {key: value for key, value in result.items() if value is not None}
