from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from .errors import safe_provider_error_message
from .telemetry import (
    OperationStatus,
    TelemetryDispatcher,
    TelemetryEvent,
    TelemetryEventType,
)

if TYPE_CHECKING:
    from litellm.integrations.custom_logger import CustomLogger as LiteLLMCustomLogger
else:
    try:
        from litellm.integrations.custom_logger import CustomLogger as LiteLLMCustomLogger
    except ImportError:

        class LiteLLMCustomLogger:
            """Provide an import-safe base when the optional LiteLLM extra is absent."""

            def __init__(self, *_args: object, **_kwargs: object) -> None:
                """Accept the initialization shape of LiteLLM's optional base class."""


class LiteLLMTelemetryCallback(LiteLLMCustomLogger):
    """Bridge LiteLLM provider callbacks into the sanitized Harbor dispatcher."""

    def __init__(self, dispatcher: TelemetryDispatcher) -> None:
        """Store the dispatcher used for provider success and failure events."""

        super().__init__()
        self.dispatcher = dispatcher

    def log_success_event(
        self,
        kwargs: dict[str, Any],
        _response_obj: Any,
        start_time: datetime,
        end_time: datetime,
    ) -> None:
        """Emit a provider-completion event through LiteLLM's sync hook."""

        self.dispatcher.emit(_event(kwargs, start_time, end_time, error=None))

    def log_failure_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: datetime,
        end_time: datetime,
    ) -> None:
        """Emit a sanitized provider-error event through LiteLLM's sync hook."""

        error = response_obj if isinstance(response_obj, Exception) else RuntimeError("failure")
        self.dispatcher.emit(_event(kwargs, start_time, end_time, error=error))

    async def async_log_success_event(
        self,
        kwargs: dict[str, Any],
        _response_obj: Any,
        start_time: datetime,
        end_time: datetime,
    ) -> None:
        """Emit a provider-completion event through LiteLLM's async hook."""

        await self.dispatcher.aemit(_event(kwargs, start_time, end_time, error=None))

    async def async_log_failure_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: datetime,
        end_time: datetime,
    ) -> None:
        """Emit a sanitized provider-error event through LiteLLM's async hook."""

        error = response_obj if isinstance(response_obj, Exception) else RuntimeError("failure")
        await self.dispatcher.aemit(_event(kwargs, start_time, end_time, error=error))


def _event(
    kwargs: dict[str, Any],
    start_time: datetime,
    end_time: datetime,
    *,
    error: Exception | None,
) -> TelemetryEvent:
    metadata = kwargs.get("litellm_params", {}).get("metadata", {})
    harbor = metadata.get("harborrag", {}) if isinstance(metadata, dict) else {}
    return TelemetryEvent(
        event_type=(
            TelemetryEventType.PROVIDER_ERROR
            if error is not None
            else TelemetryEventType.PROVIDER_COMPLETE
        ),
        operation=str(harbor.get("operation") or "model"),
        status=OperationStatus.FAILED if error is not None else OperationStatus.SUCCEEDED,
        request_id=harbor.get("request_id"),
        logical_model=harbor.get("logical_model"),
        provider_model=str(kwargs.get("model")) if kwargs.get("model") else None,
        latency_ms=max(0.0, (end_time - start_time).total_seconds() * 1_000),
        estimated_cost_usd=_nonnegative_float(kwargs.get("response_cost")),
        cache_status="hit" if kwargs.get("cache_hit") else "miss",
        error=(
            {
                "type": type(error).__name__,
                "message": safe_provider_error_message(error),
            }
            if error is not None
            else None
        ),
    )


def _nonnegative_float(value: Any) -> float | None:
    if isinstance(value, int | float) and value >= 0:
        return float(value)
    return None
