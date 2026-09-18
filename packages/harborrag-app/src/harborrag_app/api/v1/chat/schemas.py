"""Strict public chat-completion contracts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, JsonValue, StringConstraints, model_validator

from harborrag_app.api.schemas import ApiModel
from harborrag_core.models.cost import ModelCost

# Titles are trimmed and truncated by the domain (``normalize_conversation_title``)
# rather than rejected, so the schema cap is only input hygiene, well above the
# stored length.
MAX_TITLE_INPUT = 1_000
VerbatimText = Annotated[str, StringConstraints(strip_whitespace=False)]


class ChatTenantRequest(ApiModel):
    tenant: str = Field(
        default="DEFAULT",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )


class ChatSessionCreateRequest(ChatTenantRequest):
    """Create an unnamed conversation; its first successful turn supplies a title."""

    model_config = ConfigDict(json_schema_extra={"examples": [{"tenant": "DEFAULT"}]})


class ChatSessionResponse(ApiModel):
    session_id: str
    greeting: str
    title: str | None = None


class CompletionRequest(ChatTenantRequest):
    # Only the prompt is required. Keep examples free of placeholder model
    # and project names, which would be rejected when copied into a request.
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "tenant": "DEFAULT",
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
    mode: Literal["rag", "agent"] = "rag"
    max_steps: int = Field(default=4, ge=1, le=8)
    idempotency_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r".*\S.*",
        description="Stable client request key; a completed duplicate replays without a model call.",
    )
    graph_search: bool | None = None
    # Optional project scope; must exist within ``tenant`` (otherwise ``404``).
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


class ChatCompletionRequest(CompletionRequest):
    """Retrieval chat only; bounded tool runs use the agent endpoint."""

    mode: Literal["rag"] = "rag"


class ChatMessageResponse(ApiModel):
    role: Literal["assistant"]
    content: VerbatimText


class ChatCitation(ApiModel):
    document_id: str
    chunk_id: str
    score: float
    tool: str | None = Field(default=None, exclude_if=lambda value: value is None)
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


class AgentCitationValidation(ApiModel):
    complete: bool
    evidence_available: bool
    marker_count: int = Field(ge=0)
    validated_count: int = Field(ge=0)
    invalid_count: int = Field(ge=0)


class ChatUsageResponse(ApiModel):
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    cache_read_input_tokens: int | None = Field(default=None, ge=0)
    cache_creation_input_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)


class ChatToolCallResponse(ApiModel):
    step: int = Field(ge=1)
    tool: str
    ok: bool


class ChatCompletionResponse(ApiModel):
    id: str
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
    citation_validation: AgentCitationValidation | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    session_id: str
    title: str | None = None
    mode: Literal["rag", "agent"] = "rag"
    run_id: str | None = None
    stop_reason: str | None = None
    turns: int | None = Field(default=None, ge=0)
    tool_call_count: int | None = Field(default=None, ge=0)
    tool_calls: list[ChatToolCallResponse] | None = None
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


class ConversationSummary(ApiModel):
    """One conversation of the caller's own, without any message content."""

    session_id: str
    kind: Literal["chat", "agent"]
    title: str | None = None
    created_at: str
    updated_at: str
    message_count: int = Field(ge=0)


class SessionListResponse(ApiModel):
    """One page of sessions; ``next_cursor`` is absent on the last page."""

    sessions: list[ConversationSummary] = Field(default_factory=list)
    next_cursor: str | None = None


class ConversationMessageRecord(ApiModel):
    """One stored message of a conversation, as it was persisted."""

    message_id: str
    role: Literal["user", "assistant", "tool", "system"]
    content: VerbatimText
    created_at: str
    token_count: int | None = Field(default=None, ge=0)
    # Whatever the turn returned to the caller; shape follows the turn that
    # wrote it, so this is deliberately not narrowed to one citation model.
    citations: list[JsonValue] = Field(default_factory=list)
    run_id: str | None = None
    # True when a stream ended before the answer was complete: the text that
    # reached the user is kept, and marked so a reader can tell.
    partial: bool = False


class ConversationMessageListResponse(ApiModel):
    """One page of messages, oldest first; ``next_cursor`` absent at the end."""

    messages: list[ConversationMessageRecord] = Field(default_factory=list)
    next_cursor: str | None = None


class ConversationRenameRequest(ApiModel):
    """Retitle one conversation; an empty or whitespace title clears it."""

    model_config = ConfigDict(json_schema_extra={"examples": [{"title": "Release policy"}]})

    title: str = Field(default="", max_length=MAX_TITLE_INPUT)


class ConversationRenameResponse(ApiModel):
    session_id: str
    title: str | None = None
