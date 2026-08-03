"""Strict public chat-completion contracts."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from harborrag_app.api.schemas import ApiModel
from harborrag_runtime.chat import ChatPrompt

PublicChatRole = Literal["system", "developer", "user", "assistant"]


class ChatMessageRequest(ApiModel):
    role: PublicChatRole
    content: str = Field(min_length=1, max_length=65_536)


class ChatCompletionRequest(ApiModel):
    tenant: str = Field(
        default="DEFAULT",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    messages: list[ChatMessageRequest] = Field(min_length=1, max_length=100)
    prompt: ChatPrompt | None = None
    model: str | None = Field(default=None, min_length=1, max_length=128)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, gt=0, le=32_768)
    stop: str | list[str] | None = None
    seed: int | None = None

    @model_validator(mode="after")
    def validate_request_size(self) -> Self:
        if sum(len(message.content) for message in self.messages) > 131_072:
            raise ValueError("combined message content must not exceed 131072 characters")
        stops = [self.stop] if isinstance(self.stop, str) else (self.stop or [])
        if len(stops) > 4:
            raise ValueError("stop must contain at most 4 sequences")
        if any(not value or len(value) > 256 for value in stops):
            raise ValueError("each stop sequence must contain 1 to 256 characters")
        return self


class ChatMessageResponse(ApiModel):
    role: Literal["assistant"]
    content: str


class ChatUsageResponse(ApiModel):
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    cache_read_input_tokens: int | None = Field(default=None, ge=0)
    cache_creation_input_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)


class ChatCompletionResponse(ApiModel):
    id: str
    created: int | None = None
    model: str
    provider: str
    provider_model: str
    message: ChatMessageResponse
    finish_reason: str
    usage: ChatUsageResponse
    latency_ms: float | None = Field(default=None, ge=0)
    retry_count: int = Field(ge=0)
    fallback_count: int = Field(ge=0)
