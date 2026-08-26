from __future__ import annotations

from typing import Any

from .telemetry import TelemetryEvent, TelemetryEventType


class OpenTelemetryMetrics:
    """Record sanitized Harbor model counters and latency histograms through one meter."""

    def __init__(self, meter: Any) -> None:
        """Create stable instruments without owning the application meter provider."""

        self._requests = meter.create_counter(
            "harborrag.model.requests", unit="1", description="Model request attempts"
        )
        self._errors = meter.create_counter(
            "harborrag.model.errors", unit="1", description="Failed model requests"
        )
        self._tokens = meter.create_counter(
            "harborrag.model.tokens", unit="{token}", description="Model tokens"
        )
        self._retries = meter.create_counter(
            "harborrag.model.retries", unit="1", description="Model retries"
        )
        self._fallbacks = meter.create_counter(
            "harborrag.model.fallbacks", unit="1", description="Model fallbacks"
        )
        self._cache_hits = meter.create_counter(
            "harborrag.model.cache_hits", unit="1", description="Harbor cache hits"
        )
        self._duration = meter.create_histogram(
            "harborrag.model.duration", unit="ms", description="Total model duration"
        )
        self._first_token = meter.create_histogram(
            "harborrag.model.first_token", unit="ms", description="First output latency"
        )
        self._cost = meter.create_counter(
            "harborrag.model.cost", unit="USD", description="Reported model cost"
        )

    def record(self, event: TelemetryEvent) -> None:
        """Map one sanitized lifecycle event to low-cardinality metric attributes."""

        attributes = _metric_attributes(event)
        self._record_counts(event, attributes)
        self._record_timings(event, attributes)
        self._record_tokens(event, attributes)

    def _record_counts(
        self,
        event: TelemetryEvent,
        attributes: dict[str, str | bool],
    ) -> None:
        if event.event_type is TelemetryEventType.REQUEST_START:
            self._requests.add(1, attributes)
        if event.event_type in {
            TelemetryEventType.REQUEST_ERROR,
            TelemetryEventType.STREAM_ERROR,
            TelemetryEventType.PROVIDER_ERROR,
        }:
            self._errors.add(1, attributes)
        if event.retry_count:
            self._retries.add(event.retry_count, attributes)
        if event.fallback_count:
            self._fallbacks.add(event.fallback_count, attributes)
        if event.cache_status == "hit":
            self._cache_hits.add(1, attributes)

    def _record_timings(
        self,
        event: TelemetryEvent,
        attributes: dict[str, str | bool],
    ) -> None:
        if event.total_duration_ms is not None:
            self._duration.record(event.total_duration_ms, attributes)
        if event.first_token_latency_ms is not None:
            self._first_token.record(event.first_token_latency_ms, attributes)
        if event.estimated_cost_usd is not None:
            self._cost.add(event.estimated_cost_usd, attributes)

    def _record_tokens(
        self,
        event: TelemetryEvent,
        attributes: dict[str, str | bool],
    ) -> None:
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = event.usage.get(name)
            if isinstance(value, int | float) and value > 0:
                self._tokens.add(value, {**attributes, "token.type": name})


def _metric_attributes(event: TelemetryEvent) -> dict[str, str | bool]:
    raw = {
        "gen_ai.operation.name": event.operation,
        "gen_ai.provider.name": event.provider,
        "gen_ai.request.model": event.logical_model,
        "gen_ai.response.model": event.provider_model,
        "harborrag.deployment": event.deployment,
        "harborrag.streaming": event.streaming,
        "harborrag.status": event.status.value,
    }
    return {key: value for key, value in raw.items() if isinstance(value, str | bool)}
