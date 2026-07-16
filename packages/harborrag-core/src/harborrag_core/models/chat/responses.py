from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..usage import ModelTokenUsage
from .enums import FinishReason, StreamEventType
from .messages import HarborChatMessage
from .tools import HarborToolCall


class HarborChatUsage(ModelTokenUsage):
    """Represent normalized chat usage returned by a model client."""

    model_config = ConfigDict(extra="allow", frozen=True)

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    cache_read_input_tokens: int | None = Field(default=None, ge=0)
    cache_creation_input_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)


class HarborChatResponse(BaseModel):
    """Represent a normalized, provider-neutral chat response."""

    model_config = ConfigDict(extra="allow", frozen=True)

    id: str
    created: int | None = None
    logical_model: str
    provider: str
    provider_model: str
    deployment: str
    message: HarborChatMessage
    reasoning_content: str | None = None
    finish_reason: str | FinishReason = FinishReason.UNKNOWN
    usage: HarborChatUsage = Field(default_factory=HarborChatUsage)
    estimated_cost_usd: float | None = None
    latency_ms: float | None = None
    first_token_latency_ms: float | None = None
    retry_count: int = 0
    fallback_count: int = 0
    cache_hit: bool = False
    request_id: str | None = None
    provider_request_id: str | None = None
    provider_metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def text(self) -> str:
        """Return text content, or an empty string for non-text content."""
        return self.message.content if isinstance(self.message.content, str) else ""

    @property
    def tool_calls(self) -> tuple[HarborToolCall, ...]:
        """Return the tool calls requested by the model, if any."""
        return self.message.tool_calls


class HarborChatStreamChunk(BaseModel):
    """Represent one normalized event emitted by a chat response stream."""

    model_config = ConfigDict(extra="allow", frozen=True)

    event: StreamEventType
    logical_model: str
    provider: str
    provider_model: str
    deployment: str
    request_id: str | None = None
    response_id: str | None = None
    text_delta: str | None = None
    reasoning_delta: str | None = None
    tool_call_delta: HarborToolCall | None = None
    tool_calls: tuple[HarborToolCall, ...] = ()
    usage: HarborChatUsage | None = None
    finish_reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None
