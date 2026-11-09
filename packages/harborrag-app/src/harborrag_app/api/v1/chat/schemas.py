"""Strict public chat-completion contracts."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StringConstraints, field_validator, model_validator

from harborrag_app.api.schemas import ApiModel
from harborrag_core.models.cost import ModelCost

# Titles are trimmed and truncated by the domain (``normalize_conversation_title``)
# rather than rejected, so the schema cap is only input hygiene, well above the
# stored length.
MAX_TITLE_INPUT = 1_000
VerbatimText = Annotated[str, StringConstraints(strip_whitespace=False)]


class ChatTenantRequest(ApiModel):
    tenant_id: str = Field(
        default="DEFAULT",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )


class ChatSessionCreateRequest(ApiModel):
    """``POST /v1/chat/sessions`` body; every field is optional."""

    model_config = ConfigDict(json_schema_extra={"examples": [{"title": "Go candidates in Hanoi"}]})

    title: str | None = Field(
        default=None,
        max_length=MAX_TITLE_INPUT,
        description="Blank leaves the session untitled until its first answer names it",
    )
    project_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="Scope for every turn; fixed. Retrieval reads only this project's connections",
    )
    profile_id: str | None = Field(
        default=None,
        description="Assistant profile; refused until v2.1.0",
    )

    @field_validator("profile_id")
    @classmethod
    def refuse_profile(cls, value: str | None) -> str | None:
        """Refuse a profile until profiles ship in v2.1.0."""
        if value is not None:
            raise ValueError("profile_id is not available until v2.1.0")
        return value


class Session(ApiModel):
    """One chat session of the caller's own."""

    session_id: str
    title: str = Field(description="Empty until the session is named")
    project_id: str | None = Field(
        description="Fixed for the life of the session; retrieval reads only its connections"
    )
    profile_id: str | None = Field(description="null until v2.1.0")
    message_count: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime


class SessionList(ApiModel):
    """One page of the caller's sessions, newest first."""

    items: list[Session]
    next_cursor: str | None = Field(
        description="Pass as cursor to get the next page; null on the last page"
    )


class ChatCompletionRequest(ChatTenantRequest):
    # Only the prompt is required. Keep examples free of placeholder model
    # and project names, which would be rejected when copied into a request.
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "tenant_id": "DEFAULT",
                    "prompt": "What changed in the release policy?",
                    "stream": False,
                }
            ]
        }
    )

    session_id: str | None = Field(
        default=None,
        description="Existing conversation ID; omitted or null creates an unnamed conversation.",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    prompt: str = Field(min_length=1, max_length=65_536)
    stream: bool = False
    mode: Literal["rag"] = "rag"
    idempotency_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r".*\S.*",
        description="Stable client request key; a completed duplicate replays without a model call.",
    )
    graph_search: bool | None = None
    # Optional project scope; must exist within ``tenant_id`` (otherwise ``404``).
    project_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    # Optional logical model name. Bounded by the tenant's own catalog where it
    # has one and by the shared catalog otherwise; a name outside those is
    # ``422``, never a silent fall back to the default. Omitted resolves the
    # default exactly as before.
    model: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )


class ChatMessageResponse(ApiModel):
    role: Literal["assistant"]
    content: VerbatimText


class ChatCitation(ApiModel):
    document_id: str
    chunk_id: str
    score: float
    document_title: str | None = Field(default=None, exclude_if=lambda value: value is None)
    section_path: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value)
    location: str | None = Field(default=None, exclude_if=lambda value: value is None)
    marker: str | None = Field(default=None, exclude_if=lambda value: value is None)
    content: VerbatimText | None = Field(
        default=None,
        max_length=8000,
        exclude_if=lambda value: value is None,
        description="Authorized source passage, not generated text. May be absent on older runs.",
    )
    content_truncated: bool = Field(
        default=False,
        description="True when content is a bounded excerpt rather than the full passage.",
        exclude_if=lambda value: not value,
    )


class ChatUsageResponse(ApiModel):
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    cache_read_input_tokens: int | None = Field(default=None, ge=0)
    cache_creation_input_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)


class ChatCompletionResponse(ApiModel):
    completion_id: str
    created: int | None = None
    model: str
    provider: str
    provider_model: str
    message: ChatMessageResponse
    outcome: Literal["answered", "refused"] = "answered"
    refusal_reason: Literal["out_of_scope"] | None = None
    finish_reason: str
    usage: ChatUsageResponse
    cost: ModelCost = Field(default_factory=ModelCost)
    latency_ms: float | None = Field(default=None, ge=0)
    retry_count: int = Field(..., ge=0)
    fallback_count: int = Field(..., ge=0)
    citations: tuple[ChatCitation, ...] = ()
    session_id: str
    title: str | None = None
    mode: Literal["rag"] = "rag"
    # The validated project the turn was scoped to; null when none was given.
    project_id: str | None = None
    # False when the answer was produced but could not be saved to conversation
    # memory (the next prompt will not recall this turn). Never a 5xx.
    memory_persisted: bool = True

    @model_validator(mode="before")
    @classmethod
    def normalize_accounting(cls, value: object) -> object:
        # Older service results do not carry retry/fallback accounting. The
        # public response always includes the counters required by its schema.
        if isinstance(value, Mapping):
            return {"retry_count": 0, "fallback_count": 0, **value}
        return value

    @model_validator(mode="after")
    def validate_refusal(self) -> ChatCompletionResponse:
        if (self.outcome == "refused") != (self.refusal_reason is not None):
            raise ValueError("refusal_reason must be present exactly when outcome is refused")
        if self.outcome == "refused" and self.citations:
            raise ValueError("refused completions cannot cite retrieved evidence")
        return self


class Citation(ApiModel):
    """One numbered source of an answer."""

    n: int = Field(ge=1, description="The [n] marker in the answer")
    chunk_id: str
    document_id: str
    title: str
    snippet: VerbatimText = Field(description="The cited passage, up to about 300 characters")
    url: str | None = Field(description="Link to the original item")
    page: int | None = Field(description="Page in an attachment")


class Message(ApiModel):
    """One message of a session's history."""

    message_id: str
    session_id: str
    role: Literal["user", "assistant"]
    content: VerbatimText
    mode: str | None = Field(description="Assistant messages only")
    citations: list[Citation]
    trace_id: str | None
    feedback: Literal["up", "down"] | None
    created_at: datetime


class MessageList(ApiModel):
    """One page of a session's messages, newest first."""

    items: list[Message]
    next_cursor: str | None = Field(
        description="Pass as cursor to get the next page; null on the last page"
    )
