"""Control-plane ORM schemas: the 12 tables from the HTTP-backend plan §6 (ST5).

Conventions: TEXT ids (stable_hash_id output), JSON payload columns (JSONB on
Postgres via variant), timezone-aware UTC datetimes (UTCDateTime restores
tzinfo on SQLite), and a fixed naming_convention so Alembic autogenerate
stays deterministic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from harborrag_adapters.repositories.backends.sqlalchemy import UTCDateTime

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

JSONVariant = sa.JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    """Declarative base bound to the control-plane naming convention."""

    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)


class ProjectRow(Base):
    """projects: top-level grouping owning a vector collection (plan §5.1)."""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        sa.String(128), server_default="DEFAULT", nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    description: Mapped[str] = mapped_column(sa.Text, default="", nullable=False)
    collection: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    documents: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    chunks: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    size_bytes: Mapped[int] = mapped_column(sa.BigInteger, default=0, nullable=False)
    last_sync_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class SourceRow(Base):
    """sources: configured source instances; config stores secret_refs only."""

    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        sa.String(128), server_default="DEFAULT", nullable=False, index=True
    )
    project_id: Mapped[str] = mapped_column(
        sa.Text, sa.ForeignKey("projects.id"), nullable=False, index=True
    )
    source_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict, nullable=False)
    secret_refs: Mapped[list[str]] = mapped_column(JSONVariant, default=list, nullable=False)
    schedule: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    status: Mapped[str] = mapped_column(sa.Text, default="active", nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    documents: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    chunks: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    errors: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)


class SecretRefRow(Base):
    """secrets: refs + provenance, plus the Fernet-encrypted value itself."""

    __tablename__ = "secrets"

    ref: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    provider: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_by: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    ciphertext: Mapped[bytes | None] = mapped_column(sa.LargeBinary, nullable=True)


class JobRow(Base):
    """jobs: queue + history rows projecting the core Job aggregate."""

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        sa.String(128), server_default="DEFAULT", nullable=False, index=True
    )
    source_id: Mapped[str] = mapped_column(sa.Text, nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(sa.Text, nullable=False, index=True)
    job_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, default="queued", nullable=False, index=True)
    dry_run: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    attempts: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    event_sequence: Mapped[int] = mapped_column(
        sa.Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict, nullable=False)
    visibility_deadline: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    enqueued_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    documents_processed: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    chunks_created: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    errors: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)


class JobEventRow(Base):
    """job_events: ordered HarborEvent log per job (WS reconnect replay)."""

    __tablename__ = "job_events"

    job_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    seq: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    trace_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class IngestionFailureRow(Base):
    """ingestion_failures: arch-plan §6 failure-tracking contract, verbatim."""

    __tablename__ = "ingestion_failures"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(sa.Text, nullable=False, index=True)
    source_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    record_id: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    chunk_id: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    stage: Mapped[str] = mapped_column(sa.Text, nullable=False)
    error_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    error_message: Mapped[str] = mapped_column(sa.Text, nullable=False)
    retryable: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    attempt_count: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class ActivityRow(Base):
    """activity: append-only audit feed written on every mutation (plan §5.5)."""

    __tablename__ = "activity"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        sa.String(128), server_default="DEFAULT", nullable=False, index=True
    )
    actor: Mapped[str] = mapped_column(sa.Text, nullable=False)
    verb: Mapped[str] = mapped_column(sa.Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    entity_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    summary: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)


class ProviderRow(Base):
    """providers: model provider registry; key material via secret_ref only."""

    __tablename__ = "providers"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        sa.String(128), server_default="DEFAULT", nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    family: Mapped[str] = mapped_column(sa.Text, nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict, nullable=False)
    secret_ref: Mapped[str | None] = mapped_column(sa.Text, nullable=True)


class RoutingRuleRow(Base):
    """routing_rules: model routing configuration (M4 fills semantics)."""

    __tablename__ = "routing_rules"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    family: Mapped[str] = mapped_column(sa.Text, nullable=False)
    provider_id: Mapped[str] = mapped_column(sa.Text, sa.ForeignKey("providers.id"), nullable=False)
    rule_json: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class WorkspaceSettingsRow(Base):
    """workspace_settings: single JSON settings document (row id fixed to 1)."""

    __tablename__ = "workspace_settings"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        sa.String(128), server_default="DEFAULT", nullable=False, index=True
    )
    data: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class MemberRow(Base):
    """members: auth subject -> RBAC role rows (plan §8.1)."""

    __tablename__ = "members"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        sa.String(128), server_default="DEFAULT", nullable=False, index=True
    )
    subject: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True, index=True)
    role: Mapped[str] = mapped_column(sa.Text, default="reader", nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class McpQueryLogRow(Base):
    """mcp_query_log: read-side MCP telemetry written by harborrag-mcp-server."""

    __tablename__ = "mcp_query_log"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    tool: Mapped[str] = mapped_column(sa.Text, nullable=False)
    client: Mapped[str] = mapped_column(sa.Text, nullable=False)
    latency_ms: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)


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
