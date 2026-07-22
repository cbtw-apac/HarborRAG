from __future__ import annotations

from typing import Any

from harborrag_adapters.models.chat import HarborChatClientConfig
from harborrag_adapters.models.common.config import ObservabilityConfig
from harborrag_adapters.models.common.security import PrivacyConfig
from harborrag_adapters.models.common.telemetry import (
    TelemetryDispatcher,
    TelemetryEvent,
    TelemetryEventType,
)


class RecordingTelemetry:
    """Record sync and async events for contract-focused assertions."""

    def __init__(self) -> None:
        self.events: list[TelemetryEvent] = []
        self.closed = False

    def emit(self, event: TelemetryEvent) -> None:
        """Record one synchronous event."""

        self.events.append(event)

    async def aemit(self, event: TelemetryEvent) -> None:
        """Record one asynchronous event."""

        self.events.append(event)

    def close(self) -> None:
        """Record synchronous closure."""

        self.closed = True

    async def aclose(self) -> None:
        """Record asynchronous closure."""

        self.closed = True


class FailingTelemetry:
    """Raise on a selected event to verify dispatcher isolation policy."""

    def __init__(self, event_type: TelemetryEventType) -> None:
        self.event_type = event_type

    def emit(self, event: TelemetryEvent) -> None:
        """Fail only for the configured event type."""

        if event.event_type is self.event_type:
            raise RuntimeError("backend unavailable")


def chat_config(
    *,
    observability: ObservabilityConfig | None = None,
    deployments: int = 1,
    fallback: bool = False,
    attempts: int = 1,
    cache: bool = False,
) -> HarborChatClientConfig:
    """Build a compact chat configuration for telemetry tests."""

    models: dict[str, Any] = {
        "primary": {
            "aliases": ["friendly-chat"],
            "fallbacks": ["secondary"] if fallback else [],
            "deployments": [
                {
                    "name": f"primary-{index}",
                    "provider": "openai",
                    "model": f"openai/model-{index}",
                    "api_key": "key",
                    "order": index,
                }
                for index in range(deployments)
            ],
        }
    }
    if fallback:
        models["secondary"] = {
            "provider": "openai",
            "model": "openai/fallback",
            "api_key": "key",
        }
    return HarborChatClientConfig.model_validate(
        {
            "default_model": "primary",
            "retry": {
                "same_deployment_attempts": attempts,
                "max_deployment_failovers": 5,
                "max_model_fallbacks": 5,
                "base_delay_seconds": 0,
                "max_delay_seconds": 0,
            },
            "routing": {"strategy": "ordered"},
            "cache": {"enabled": cache},
            "observability": observability or ObservabilityConfig(),
            "models": models,
        }
    )


def telemetry_dispatcher(sink: object, privacy: PrivacyConfig | None = None) -> TelemetryDispatcher:
    """Build an enabled dispatcher around one test sink."""

    return TelemetryDispatcher(
        [sink], config=ObservabilityConfig(privacy=privacy or PrivacyConfig())
    )


def recorded_event(sink: RecordingTelemetry, event_type: TelemetryEventType) -> TelemetryEvent:
    """Return the first recorded event of the requested type."""

    return next(event for event in sink.events if event.event_type is event_type)
