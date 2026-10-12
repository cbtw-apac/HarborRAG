"""Strict public contracts for long-term memory administration."""

from __future__ import annotations

from typing import Literal

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
    """What a session erasure removed, per category.

    ``status`` is ``completed``, or ``deferred`` when the session and its
    memories are gone but ``deferred_index_points`` vector deletes are queued
    for automatic retry.
    """

    memories: int = Field(ge=0)
    index_points: int = Field(ge=0)
    sessions: int = Field(ge=0)
    conversation_messages_cleared: int = Field(ge=0)
    agent_run_checkpoints: int = Field(ge=0)
    deferred_index_points: int = Field(ge=0)
    status: Literal["completed", "deferred"]


class SessionErasureResponse(ErasureCounts):
    session_id: str


class UserErasureResponse(ApiModel):
    """What one right-to-erasure run removed.

    ``status`` is ``completed``, or ``deferred`` (HTTP 202) when every store
    was erased but some vector deletes are queued for automatic retry. A run
    with a failed step is a 409 error naming ``erasure_id``, never this body.
    """

    user_id: str
    erasure_id: str
    status: Literal["completed", "deferred"]
    sessions: int = Field(ge=0)
    memories: int = Field(ge=0)
    index_points: int = Field(ge=0)
    deferred_index_points: int = Field(ge=0)
    agent_run_checkpoints: int = Field(ge=0)
    completion_requests: int = Field(ge=0)
    model_usage_pseudonymized: int = Field(ge=0)
