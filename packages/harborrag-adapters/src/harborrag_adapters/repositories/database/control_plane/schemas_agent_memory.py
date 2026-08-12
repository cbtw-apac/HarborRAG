"""Conversation/agent-run/long-term-memory ORM rows.

Split out of schemas.py to keep that file under the repo's file-length gate;
these three tables (conversation sessions + turns, agent-run checkpoints,
long-term memory) form one cohesive cluster consumed by conversation.py,
agent_runs.py, and memory.py respectively. schemas.py imports this module
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
    """Persisted authenticated chat/agent session resource."""

    __tablename__ = "conversation_sessions"

    session_id: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    principal_id: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ConversationMemoryRow(Base):
    """Completed chat/agent turns isolated by authenticated session identity."""

    __tablename__ = "conversation_memory"
    __table_args__ = (
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
    session_id: Mapped[str] = mapped_column(
        sa.ForeignKey("conversation_sessions.session_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    assistant_content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class AgentRunRow(Base):
    """Checkpointed agent-run state: resumable, optimistic-concurrency versioned.

    ``state_json`` holds the parts of the run that only ever get replaced as a
    whole (messages, executions, usage, response) -- splitting those into
    columns would not make any query cheaper, since a checkpoint write always
    replaces the entire run state at once.
    """

    __tablename__ = "agent_runs"

    run_id: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(sa.String(128), nullable=False, index=True)
    principal_id: Mapped[str] = mapped_column(sa.String(512), nullable=False)
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


class MemoryRow(Base):
    """Canonical long-term memory record, scoped by owner and ``scope``.

    ``tenant_id``/``project_id``/``principal_id``/``session_id``/``run_id``
    mirror ``MemoryOwner`` field-for-field so a query's scope filter can
    reference these columns by the same names ``visible_to`` checks in
    ``harborrag_core.ports.memory`` -- the two are kept in agreement by
    construction, not by convention.
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
    )

    memory_id: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    scope: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    memory_type: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(sa.String(128), nullable=False, index=True)
    project_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    principal_id: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    session_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    run_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict, nullable=False)
    importance: Mapped[float] = mapped_column(sa.Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
