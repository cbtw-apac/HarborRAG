"""Strict public contracts for long-term memory administration."""

from __future__ import annotations

from pydantic import Field

from harborrag_app.api.schemas import ApiModel


class MemoryRecord(ApiModel):
    """One stored memory with the provenance it was extracted from."""

    memory_id: str
    scope: str
    memory_type: str
    content: str
    importance: float = Field(ge=0.0, le=1.0)
    # Bitemporal validity: a superseded fact stays readable as history, with
    # ``invalid_at`` set and ``superseded_by`` naming its replacement.
    valid_from: str | None = None
    invalid_at: str | None = None
    superseded_by: str | None = None
    source_session_id: str | None = None
    source_message_ids: list[str] = Field(default_factory=list)
    entity_ids: list[str] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None


class MemoryListResponse(ApiModel):
    memories: list[MemoryRecord] = Field(default_factory=list)


class MemoryDeletionResponse(ApiModel):
    memory_id: str
    deleted: bool


class ErasureCounts(ApiModel):
    """What an erasure removed, per category.

    ``agent_run_checkpoints`` is ``0`` when the configured checkpoint store
    offers no owner-scoped delete -- the endpoint reports what it removed and
    never implies more.
    """

    memories: int = Field(ge=0)
    index_points: int = Field(ge=0)
    sessions: int = Field(ge=0)
    conversation_messages_cleared: int = Field(ge=0)
    agent_run_checkpoints: int = Field(ge=0)


class SessionErasureResponse(ErasureCounts):
    session_id: str


class UserErasureResponse(ErasureCounts):
    user_id: str
