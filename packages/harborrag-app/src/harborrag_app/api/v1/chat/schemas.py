"""Strict public chat-completion contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from harborrag_app.api.schemas import ApiModel


class ChatSessionCreateRequest(ApiModel):
    tenant: str = Field(
        default="DEFAULT",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )


class ChatSessionResponse(ApiModel):
    session_id: str
    greeting: str


class ChatCompletionRequest(ChatSessionCreateRequest):
    session_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    prompt: str = Field(min_length=1, max_length=65_536)
    stream: bool = False
    graph_search: bool | None = None


class ChatMessageResponse(ApiModel):
    role: Literal["assistant"]
    content: str


class ChatCitation(ApiModel):
    document_id: str
    chunk_id: str
    score: float


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
    citations: tuple[ChatCitation, ...] = ()
    session_id: str
