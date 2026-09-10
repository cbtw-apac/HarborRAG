"""Conversation/agent-run/long-term-memory ORM rows.

Split out of schemas.py to keep that file under the repo's file-length gate;
these tables (conversation sessions + messages, the read-only legacy turn
table, agent-run checkpoints, long-term memory) form one cohesive cluster
consumed by conversation.py, agent_runs.py, and memory.py respectively. schemas.py imports this module
at the bottom so every class here still registers on the shared ``Base``
metadata whenever schemas.py is imported -- required for Alembic
autogenerate and the metadata-drift test to see these tables.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from harborrag_adapters.repositories.backends.sqlalchemy import UTCDateTime

from .schemas import Base, JSONVariant


class ConversationSessionRow(Base):
    """Persisted authenticated chat/agent session resource.

    ``user_id`` (migration 0024) is the human who owns the conversation and
    is load-bearing in every predicate; ``principal_id`` is retained purely
    as the credential that created the session. ``updated_at`` tracks the
    last append so a conversation listing can sort by recency.
    """

    __tablename__ = "conversation_sessions"
    __table_args__ = (
        sa.Index(
            "ix_conversation_sessions_user_updated",
            "tenant_id",
            "user_id",
            "updated_at",
        ),
    )

    session_id: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    principal_id: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    user_id: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    # "chat" | "agent": the surface that created the session; completions on
    # the other surface treat the session as unknown (see ConversationKind).
    kind: Mapped[str] = mapped_column(sa.String(16), nullable=False, server_default="chat")
    title: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ConversationMessageRow(Base):
    """One persisted conversation message (user/assistant/tool/system).

    ``seq`` is a per-session sequence assigned by the repository at append
    time; it is the ordering key, with ``created_at``/``message_id`` only as
    tie-breakers. Migration 0022 backfilled pairs from the legacy turn table
    with ``seq`` derived from the legacy row id.
    """

    __tablename__ = "conversation_messages"
    __table_args__ = (
        # Migration 0024 replaced the principal-led identity index with these
        # two: reads are always scoped by (tenant, user, session), and the
        # session-led index keeps the cascade/lookup-by-session path cheap.
        sa.Index(
            "ix_conversation_messages_user_seq",
            "tenant_id",
            "user_id",
            "session_id",
            "seq",
        ),
        sa.Index(
            "ix_conversation_messages_session_seq",
            "session_id",
            "seq",
        ),
    )

    message_id: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    principal_id: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    user_id: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    session_id: Mapped[str] = mapped_column(
        sa.ForeignKey("conversation_sessions.session_id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    token_count: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    tool_calls_json: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    citations_json: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    run_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    partial: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    seq: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)


class ConversationMemoryLegacyRow(Base):
    """Pre-0022 completed turns, renamed and kept read-only for one release.

    Nothing writes here any more: 0022 copied every row into
    ``conversation_messages``. The constraint and index names are the ones
    the table carried before the rename so ORM metadata matches the
    migrated database on every dialect.
    """

    __tablename__ = "conversation_memory_legacy"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["conversation_sessions.session_id"],
            name="fk_conversation_memory_session_id_conversation_sessions",
            ondelete="CASCADE",
        ),
        sa.Index(
            "ix_conversation_memory_identity_created",
            "tenant_id",
            "principal_id",
            "session_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    principal_id: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    session_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    user_content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    assistant_content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class AgentRunRow(Base):
    """Checkpointed agent-run state: resumable, optimistic-concurrency versioned.

    ``state_json`` holds the parts of the run that only ever get replaced as a
    whole (messages, executions, usage, response) -- splitting those into
    columns would not make any query cheaper, since a checkpoint write always
    replaces the entire run state at once.

    ``user_id`` (migration 0027) is the human who owns the run, matching the
    ``conversation_sessions`` parent: every predicate is scoped by
    ``(tenant_id, user_id, session_id)`` and ``principal_id`` is retained
    only as the credential that acted. ``run_id`` stays the primary key, so
    the run-id lookup path is unchanged.
    """

    __tablename__ = "agent_runs"
    __table_args__ = (
        # Migration 0027 replaced the redundant tenant-only index with this
        # one: every read is scoped by (tenant, user, session), and that is
        # a covering prefix for a tenant-led scan.
        sa.Index(
            "ix_agent_runs_user_session",
            "tenant_id",
            "user_id",
            "session_id",
        ),
    )

    run_id: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    principal_id: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    user_id: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    session_id: Mapped[str] = mapped_column(
        sa.ForeignKey("conversation_sessions.session_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    step: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    stop_reason: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    state_json: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    # Executor lease (migration 0020): set while RUNNING, refreshed on every
    # save_step, cleared by terminal statuses. A RUNNING row whose lease has
    # lapsed is a crashed worker and may be claimed by ``resume``.
    lease_owner: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class MemoryRow(Base):
    """Canonical long-term memory record, scoped by owner and ``scope``.

    ``tenant_id``/``project_id``/``user_id``/``principal_id``/``session_id``/
    ``run_id`` mirror ``MemoryOwner`` field-for-field so a query's scope
    filter can reference these columns by the same names ``visible_to``
    checks in ``harborrag_core.ports.memory`` -- the two are kept in
    agreement by construction, not by convention.
    """

    __tablename__ = "memories"
    __table_args__ = (
        sa.Index(
            "ix_memories_owner",
            "tenant_id",
            "project_id",
            "principal_id",
            "session_id",
            "run_id",
        ),
        sa.Index("ix_memories_tenant_user", "tenant_id", "user_id"),
        sa.Index("ix_memories_content_hash", "content_hash"),
    )

    memory_id: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    scope: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    memory_type: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(sa.String(128), nullable=False, index=True)
    project_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    user_id: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    principal_id: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    session_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    run_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict, nullable=False)
    importance: Mapped[float] = mapped_column(sa.Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # Provenance and bitemporal validity (migration 0023).
    valid_from: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    invalid_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    source_session_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    source_message_ids_json: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    entity_ids_json: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
