from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from harborrag_core.models.chat import (
    FinishReason,
    HarborChatStreamChunk,
    HarborChatUsage,
    HarborToolCall,
    StreamEventType,
)
from harborrag_core.models.errors import HarborChatError, HarborChatProviderError

from harborrag_core.models.common.responses import coerce_sdk_mapping as coerce_mapping
from .configs import HarborChatProviderConfig
from .normalization import (
    normalize_chat_usage,
    normalize_finish_reason,
    normalize_tool_call_delta,
    normalize_tool_calls,
)


@dataclass(slots=True)
class ToolCallBuffer:
    """Accumulate fragments for one indexed provider tool call."""

    index: int
    call_id: str = ""
    call_type: str = "function"
    name_parts: list[str] = field(default_factory=list)
    argument_parts: list[str] = field(default_factory=list)

    def add(self, delta: HarborToolCall) -> None:
        """Append one normalized delta without duplicating stable identity fields."""

        if delta.id and not self.call_id:
            self.call_id = delta.id
        if delta.type:
            self.call_type = delta.type
        if delta.function.name:
            self.name_parts.append(delta.function.name)
        if delta.function.arguments:
            self.argument_parts.append(delta.function.arguments)

    def build(self) -> HarborToolCall:
        """Build the complete call and parse its assembled JSON arguments."""

        return normalize_tool_calls(
            [
                {
                    "id": self.call_id,
                    "type": self.call_type,
                    "index": self.index,
                    "function": {
                        "name": "".join(self.name_parts),
                        "arguments": "".join(self.argument_parts),
                    },
                }
            ]
        )[0]


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
        self._tool_calls: dict[int, ToolCallBuffer] = {}

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
        events.extend(self._content_events(delta))
        events.extend(self._tool_events(delta))
        if choice.get("finish_reason") is not None:
            self.finish_reason = normalize_finish_reason(choice["finish_reason"])
        return tuple(events)

    def complete(self) -> HarborChatStreamChunk:
        """Create the final event with assembled parallel tool calls and usage."""

        tool_calls = tuple(self._tool_calls[index].build() for index in sorted(self._tool_calls))
        return self._event(
            StreamEventType.COMPLETED,
            usage=self.usage,
            tool_calls=tool_calls,
            finish_reason=self.finish_reason.value,
        )

    def error(self, error: HarborChatError) -> HarborChatStreamChunk:
        """Create a sanitized error event before the typed exception is raised."""

        return self._event(StreamEventType.ERROR, error=error.to_dict())

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
            index = call.index if call.index is not None else position
            buffer = self._tool_calls.setdefault(index, ToolCallBuffer(index=index))
            buffer.add(call)
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
