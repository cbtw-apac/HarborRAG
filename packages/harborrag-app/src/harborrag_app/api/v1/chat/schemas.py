"""Strict public chat-completion contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, JsonValue

from harborrag_app.api.schemas import ApiModel

# Titles are trimmed and truncated by the domain (``normalize_conversation_title``)
# rather than rejected, so the schema cap is only input hygiene, well above the
# stored length.
MAX_TITLE_INPUT = 1_000


class ChatTenantRequest(ApiModel):
    tenant: str = Field(
        default="DEFAULT",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )


class ChatSessionCreateRequest(ChatTenantRequest):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"tenant": "DEFAULT", "title": "Release policy"}]}
    )

    # Names the conversation at creation so a client need not create then
    # rename. Omitted or blank leaves it unnamed.
    title: str | None = Field(default=None, max_length=MAX_TITLE_INPUT)


class ChatSessionResponse(ApiModel):
    session_id: str
    greeting: str


class ChatCompletionRequest(ChatTenantRequest):
    # Only ``session_id`` and ``prompt`` are required, so the example sends
    # exactly those two plus the tenant. Without an explicit example the docs
    # generate a value for every optional field from its pattern, and pasting
    # those back names a project and a model that do not exist.
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "tenant": "DEFAULT",
                    "session_id": "session-0b9c1f2e3d4a5b6c7d8e9f0a1b2c3d4e",
                    "prompt": "What changed in the release policy?",
                    "stream": False,
                }
            ]
        }
    )

    session_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    prompt: str = Field(min_length=1, max_length=65_536)
    stream: bool = False
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
    # The validated project the turn was scoped to; null when none was given.
    project_id: str | None = None
    # False when the answer was produced but could not be saved to conversation
    # memory (the next prompt will not recall this turn). Never a 5xx.
    memory_persisted: bool = True


class ConversationSummary(ApiModel):
    """One conversation of the caller's own, without any message content."""

    session_id: str
    kind: Literal["chat", "agent"]
    title: str | None = None
    created_at: str
    updated_at: str
    message_count: int = Field(ge=0)


class ConversationListResponse(ApiModel):
    """One page of conversations; ``next_cursor`` is absent on the last page."""

    conversations: list[ConversationSummary] = Field(default_factory=list)
    next_cursor: str | None = None


class ConversationMessageRecord(ApiModel):
    """One stored message of a conversation, as it was persisted."""

    message_id: str
    role: Literal["user", "assistant", "tool", "system"]
    content: str
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
