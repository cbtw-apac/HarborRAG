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
