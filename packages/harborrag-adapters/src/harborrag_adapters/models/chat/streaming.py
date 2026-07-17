from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

from harborrag_core.models.chat import (
    FinishReason,
    HarborChatStreamChunk,
    HarborChatUsage,
    StreamEventType,
)
from harborrag_core.models.errors import HarborChatError, HarborChatProviderError

from harborrag_adapters.models.common.responses import coerce_sdk_mapping as coerce_mapping

from .configs import HarborChatProviderConfig
from .normalization import (
    normalize_chat_usage,
    normalize_finish_reason,
    normalize_tool_call_delta,
)
from .reasoning import normalize_reasoning_delta
from .tool_assembly import StreamingToolCallAssembler


class ChatStreamNormalizer:
    """Convert LiteLLM chunks into stable events while assembling tool calls."""

    def __init__(
        self,
        *,
        logical_model: str,
        deployment: HarborChatProviderConfig,
        request_id: str,
    ) -> None:
        """Store stable operation identity and initialize stream state."""

        self.logical_model = logical_model
        self.deployment = deployment
        self.request_id = request_id
        self.response_id: str | None = None
        self.provider_model = deployment.model
        self.finish_reason = FinishReason.UNKNOWN
        self.usage: HarborChatUsage | None = None
        self._tool_calls = StreamingToolCallAssembler()
        self._metadata_emitted = False
        self._metadata: dict[str, Any] = {}
        self._started_at = time.perf_counter()
        self._first_output_latency_ms: float | None = None

    def consume(self, raw: Any) -> tuple[HarborChatStreamChunk, ...]:
        """Normalize every event represented by one provider stream chunk."""

        data = coerce_mapping(raw)
        if not data:
            raise self._malformed("expected a stream chunk mapping")
        if data.get("id") is not None:
            self.response_id = str(data["id"])
        if data.get("model") is not None:
            self.provider_model = str(data["model"])

        events: list[HarborChatStreamChunk] = []
        events.extend(self._metadata_events(data))
        if data.get("usage") is not None:
            self.usage = normalize_chat_usage(data["usage"])
            events.append(self._event(StreamEventType.USAGE, usage=self.usage))

        choices = data.get("choices")
        if choices is None:
            if data.get("usage") is None:
                raise self._malformed("missing choices")
            return tuple(events)
        if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)):
            raise self._malformed("invalid choices")
        if not choices:
            if data.get("usage") is None:
                raise self._malformed("empty choices without usage")
            return tuple(events)

        choice = coerce_mapping(choices[0])
        if not choice:
            raise self._malformed("invalid first choice")
        delta = coerce_mapping(choice.get("delta"))
        output_events = [
            *self._reasoning_events(delta),
            *self._content_events(delta),
            *self._tool_events(delta),
        ]
        if output_events and self._first_output_latency_ms is None:
            self._first_output_latency_ms = self._elapsed_ms()
        events.extend(output_events)
        if choice.get("finish_reason") is not None:
            self.finish_reason = normalize_finish_reason(choice["finish_reason"])
            events.append(
                self._event(
                    StreamEventType.METADATA,
                    finish_reason=self.finish_reason.value,
                    metadata={"finish_reason": self.finish_reason.value},
                )
            )
        return tuple(events)

    def complete(self) -> HarborChatStreamChunk:
        """Create the final event with assembled calls, usage, timing, and finish metadata."""

        metadata = {
            **self._metadata,
            "finish_reason": self.finish_reason.value,
            "stream_duration_ms": self._elapsed_ms(),
        }
        if self._first_output_latency_ms is not None:
            metadata["first_token_latency_ms"] = self._first_output_latency_ms
        return self._event(
            StreamEventType.COMPLETED,
            usage=self.usage,
            tool_calls=self._tool_calls.completed_calls(),
            finish_reason=self.finish_reason.value,
            metadata=metadata,
        )

    def error(self, error: HarborChatError) -> HarborChatStreamChunk:
        """Create a sanitized error event before the typed exception is raised."""

        return self._event(
            StreamEventType.ERROR,
            error=error.to_dict(),
            metadata={"stream_duration_ms": self._elapsed_ms()},
        )

    def _metadata_events(self, data: Mapping[str, Any]) -> list[HarborChatStreamChunk]:
        updates = {
            name: data[name]
            for name in ("created", "system_fingerprint", "service_tier")
            if data.get(name) is not None
        }
        if data.get("model") is not None:
            updates["provider_model"] = str(data["model"])
        unchanged = self._metadata_emitted and all(
            self._metadata.get(name) == value for name, value in updates.items()
        )
        if not updates or unchanged:
            return []
        self._metadata.update(updates)
        self._metadata_emitted = True
        return [self._event(StreamEventType.METADATA, metadata=dict(updates))]

    def _reasoning_events(self, delta: Mapping[str, Any]) -> list[HarborChatStreamChunk]:
        reasoning = normalize_reasoning_delta(delta)
        if not reasoning:
            return []
        return [self._event(StreamEventType.REASONING_DELTA, reasoning_delta=reasoning)]

    def _content_events(self, delta: Mapping[str, Any]) -> list[HarborChatStreamChunk]:
        content = delta.get("content")
        if content is None or content == "":
            return []
        if not isinstance(content, str):
            raise self._malformed("text delta must be a string")
        return [self._event(StreamEventType.TEXT_DELTA, text_delta=content)]

    def _tool_events(self, delta: Mapping[str, Any]) -> list[HarborChatStreamChunk]:
        values = delta.get("tool_calls")
        legacy = coerce_mapping(delta.get("function_call"))
        if values is None and legacy:
            values = [{"id": "function_call", "index": 0, "function": legacy}]
        if values is None:
            return []
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            raise self._malformed("tool-call deltas must be a sequence")
        events: list[HarborChatStreamChunk] = []
        for position, value in enumerate(values):
            call = normalize_tool_call_delta(value, fallback_index=position)
            self._tool_calls.add(call, fallback_index=position)
            events.append(self._event(StreamEventType.TOOL_CALL_DELTA, tool_call_delta=call))
        return events

    def _event(self, event: StreamEventType, **values: Any) -> HarborChatStreamChunk:
        return HarborChatStreamChunk(
            event=event,
            logical_model=self.logical_model,
            provider=self.deployment.provider.value,
            provider_model=self.provider_model,
            deployment=self.deployment.name,
            request_id=self.request_id,
            response_id=self.response_id,
            **values,
        )

    def _elapsed_ms(self) -> float:
        return (time.perf_counter() - self._started_at) * 1_000

    def _malformed(self, detail: str) -> HarborChatProviderError:
        return HarborChatProviderError(
            f"malformed provider stream: {detail}",
            operation="chat",
            provider=self.deployment.provider.value,
            logical_model=self.logical_model,
            provider_model=self.provider_model,
            deployment=self.deployment.name,
            request_id=self.request_id,
            retryable=False,
        )
