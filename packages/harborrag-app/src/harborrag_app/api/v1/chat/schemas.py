"""Strict public chat-completion contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, StringConstraints, field_validator, model_validator

from harborrag_app.api.schemas import ApiModel

# Titles are trimmed and truncated by the domain (``normalize_conversation_title``)
# rather than rejected, so the schema cap is only input hygiene, well above the
# stored length.
MAX_TITLE_INPUT = 1_000
VerbatimText = Annotated[str, StringConstraints(strip_whitespace=False)]


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


ProjectRef = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
]
ConnectionRef = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=192,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*/[a-z0-9][a-z0-9_-]{0,62}$",
    ),
]


class Condition(ApiModel):
    """One condition on a filterable field."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"field": "connection", "op": "in", "value": ["cpm/cpm-tickets"]}]
        }
    )

    field: str = Field(min_length=1, max_length=128, description="A filterable field")
    op: Literal["eq", "ne", "in", "nin", "gt", "gte", "lt", "lte", "contains"]
    value: Any = Field(description="A value, or a list of values for in and nin")


class Filter(ApiModel):
    """Exactly one of and / or. Items are conditions or nested filters."""

    model_config = ConfigDict(
        populate_by_name=False,
        json_schema_extra={
            "examples": [
                {
                    "and": [
                        {"field": "connection", "op": "in", "value": ["cpm/cpm-tickets"]},
                        {"field": "document_kind", "op": "eq", "value": "jira_issue"},
                    ]
                }
            ]
        },
    )

    all_of: list[Condition | Filter] | None = Field(default=None, alias="and")
    any_of: list[Condition | Filter] | None = Field(default=None, alias="or")

    @model_validator(mode="after")
    def one_combinator(self) -> Filter:
        """Require exactly one of ``and`` and ``or``."""
        if (self.all_of is None) == (self.any_of is None):
            raise ValueError("a filter has exactly one of and, or")
        return self


class ChatCompletionRequest(ApiModel):
    """``POST /v1/chat/completions`` body."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "message": "Who has Go and Kubernetes experience in Hanoi?",
                    "mode": "classic",
                    "stream": False,
                }
            ]
        }
    )

    session_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        description="Leave out for a one-off question",
    )
    message: VerbatimText = Field(min_length=1, max_length=8000)
    mode: Literal["instant", "classic", "advanced", "auto"] = Field(
        default="classic",
        description="instant or classic; advanced and auto are refused until v2.1.0",
    )
    reasoning: Literal["off", "low", "high"] = Field(
        default="off", description="Thinking budget; only off until v2.1.0"
    )
    filters: Filter | None = None
    project_ids: list[ProjectRef] | None = Field(
        default=None,
        min_length=1,
        max_length=20,
        description="One-off questions only; a session uses its project. Retrieval reads "
        "only these projects' connections",
    )
    connections: list[ConnectionRef] | None = Field(
        default=None,
        min_length=1,
        max_length=50,
        description="Connection refs (project_id/connection_id) inside the scope",
    )
    stream: bool = Field(default=False, description="true answers with text/event-stream")

    @field_validator("message")
    @classmethod
    def refuse_blank_message(cls, value: str) -> str:
        """Refuse a message of whitespace only, keeping any other message verbatim."""
        if not value.strip():
            raise ValueError("message must contain text")
        return value

    @field_validator("reasoning")
    @classmethod
    def refuse_reasoning(cls, value: str) -> str:
        """Refuse a thinking budget until reasoning ships in v2.1.0."""
        if value != "off":
            raise ValueError("reasoning other than off is not available until v2.1.0")
        return value


class Usage(ApiModel):
    """What the answer cost."""

    mode: Literal["instant", "classic", "advanced"]
    reasoning: Literal["off", "low", "high"]
    steps: int = Field(ge=0, description="Retrieval steps")
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(ge=0)
    cost_usd: float | None


class ChatCompletionResponse(ApiModel):
    """An answered question; ``completion_id`` is the stored assistant ``message_id``."""

    completion_id: str
    session_id: str | None = Field(description="null for a one-off question")
    trace_id: str
    mode: str
    answer: VerbatimText
    citations: list[Citation]
    usage: Usage


Filter.model_rebuild()


class MessageList(ApiModel):
    """One page of a session's messages, newest first."""

    items: list[Message]
    next_cursor: str | None = Field(
        description="Pass as cursor to get the next page; null on the last page"
    )
