from __future__ import annotations

from enum import StrEnum


class MessageRole(StrEnum):
    """Enumerate supported message role values."""

    SYSTEM = "system"
    DEVELOPER = "developer"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class FinishReason(StrEnum):
    """Enumerate supported finish reason values."""

    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"
    CONTENT_FILTER = "content_filter"
    ERROR = "error"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value: object) -> FinishReason:
        """Map any provider value onto the enum, unrecognized ones to UNKNOWN.

        A finish reason is metadata about a completed call, so an unfamiliar
        one must not turn a successful response into a validation error.
        """

        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            return cls.UNKNOWN


class StreamEventType(StrEnum):
    """Enumerate supported stream event type values."""

    TEXT_DELTA = "text_delta"
    REASONING_DELTA = "reasoning_delta"
    TOOL_CALL_DELTA = "tool_call_delta"
    USAGE = "usage"
    METADATA = "metadata"
    COMPLETED = "completed"
    ERROR = "error"


class StructuredOutputDegradation(StrEnum):
    """Control how structured output degrades when native schemas are unavailable."""

    REJECT = "reject"
    JSON_MODE = "json_mode"
    PROMPT = "prompt"
