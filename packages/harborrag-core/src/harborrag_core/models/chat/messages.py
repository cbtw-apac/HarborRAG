from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import field_validator, model_validator

from harborrag_core.base import StrictModel

from .content import ContentPart
from .enums import MessageRole
from .tools import HarborToolCall


class HarborChatMessage(StrictModel):
    """Represent a provider-neutral chat message with optional multimodal content."""

    role: MessageRole
    content: str | tuple[ContentPart, ...] | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[HarborToolCall, ...] = ()
    context_kind: Literal["user", "retrieved_context"] | None = None

    @field_validator("content", mode="before")
    @classmethod
    def normalize_content_parts(cls, value: Any) -> Any:
        """Coerce a list of content parts into a tuple before validation."""
        if isinstance(value, list):
            return tuple(value)
        return value

    @model_validator(mode="after")
    def validate_content_parts(self) -> Self:
        """Require non-empty multimodal parts on user messages only."""

        if not isinstance(self.content, tuple):
            return self
        if not self.content:
            raise ValueError("multimodal content must contain at least one part")
        if self.role is not MessageRole.USER:
            raise ValueError("multimodal content is supported only for user messages")
        return self

    @classmethod
    def system(cls, content: str) -> Self:
        """Build a system message with the given text content."""
        return cls(role=MessageRole.SYSTEM, content=content)

    @classmethod
    def developer(cls, content: str) -> Self:
        """Build a developer message with the given text content."""
        return cls(role=MessageRole.DEVELOPER, content=content)

    @classmethod
    def user(cls, content: str | list[ContentPart] | tuple[ContentPart, ...]) -> Self:
        """Build a user message from text or multimodal content parts."""
        return cls(
            role=MessageRole.USER,
            content=tuple(content) if isinstance(content, list) else content,
        )

    @classmethod
    def retrieved_context(cls, content: str, *, name: str | None = None) -> Self:
        """Create context that may be truncated without altering the user message."""
        return cls(
            role=MessageRole.USER,
            content=content,
            name=name,
            context_kind="retrieved_context",
        )

    @classmethod
    def assistant(
        cls,
        content: str | None = None,
        *,
        tool_calls: list[HarborToolCall] | tuple[HarborToolCall, ...] = (),
    ) -> Self:
        """Build an assistant message with optional text content and tool calls."""
        return cls(role=MessageRole.ASSISTANT, content=content, tool_calls=tuple(tool_calls))

    @classmethod
    def tool(cls, content: str, *, tool_call_id: str, name: str | None = None) -> Self:
        """Build a tool-result message responding to a specific tool call."""
        return cls(role=MessageRole.TOOL, content=content, tool_call_id=tool_call_id, name=name)
