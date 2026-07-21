"""0001: all 12 control-plane tables (plan §6).

Frozen DDL — later migrations diff against this baseline, so it must never
be edited to track models.py; add a new revision instead.

Revision ID: 0001
Revises: None
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_JSON = sa.JSON()
_TZDT = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Create the full v1 control-plane schema."""
    op.create_table(
        "projects",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("collection", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", _TZDT, nullable=False),
        sa.Column("updated_at", _TZDT, nullable=False),
        sa.Column("documents", sa.Integer(), nullable=False),
        sa.Column("chunks", sa.Integer(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("last_sync_at", _TZDT, nullable=True),
    )
    op.create_table(
        "sources",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Text(),
            sa.ForeignKey("projects.id", name="fk_sources_project_id_projects"),
            nullable=False,
        ),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("config_json", _JSON, nullable=False),
        sa.Column("secret_refs", _JSON, nullable=False),
        sa.Column("schedule", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("last_run_at", _TZDT, nullable=True),
        sa.Column("documents", sa.Integer(), nullable=False),
        sa.Column("chunks", sa.Integer(), nullable=False),
        sa.Column("errors", sa.Integer(), nullable=False),
    )
    op.create_index("ix_sources_project_id", "sources", ["project_id"])
    op.create_table(
        "secrets",
        sa.Column("ref", sa.Text(), primary_key=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", _TZDT, nullable=False),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("job_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("payload_json", _JSON, nullable=False),
        sa.Column("visibility_deadline", _TZDT, nullable=True),
        sa.Column("enqueued_at", _TZDT, nullable=False),
        sa.Column("started_at", _TZDT, nullable=True),
        sa.Column("finished_at", _TZDT, nullable=True),
        sa.Column("documents_processed", sa.Integer(), nullable=False),
        sa.Column("chunks_created", sa.Integer(), nullable=False),
        sa.Column("errors", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
    )
    op.create_index("ix_jobs_source_id", "jobs", ["source_id"])
    op.create_index("ix_jobs_project_id", "jobs", ["project_id"])
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_table(
        "job_events",
        sa.Column("job_id", sa.Text(), primary_key=True),
        sa.Column("seq", sa.Integer(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column("payload_json", _JSON, nullable=False),
        sa.Column("created_at", _TZDT, nullable=False),
    )
    op.create_table(
        "ingestion_failures",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("record_id", sa.Text(), nullable=True),
        sa.Column("chunk_id", sa.Text(), nullable=True),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("error_type", sa.Text(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", _TZDT, nullable=False),
        sa.Column("last_seen_at", _TZDT, nullable=False),
        sa.Column("resolved_at", _TZDT, nullable=True),
    )
    op.create_index("ix_ingestion_failures_run_id", "ingestion_failures", ["run_id"])
    op.create_table(
        "activity",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("verb", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_at", _TZDT, nullable=False),
    )
    op.create_index("ix_activity_created_at", "activity", ["created_at"])
    op.create_table(
        "providers",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("family", sa.Text(), nullable=False),
        sa.Column("config_json", _JSON, nullable=False),
        sa.Column("secret_ref", sa.Text(), nullable=True),
    )
    op.create_table(
        "routing_rules",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("family", sa.Text(), nullable=False),
        sa.Column(
            "provider_id",
            sa.Text(),
            sa.ForeignKey(
                "providers.id", name="fk_routing_rules_provider_id_providers"
            ),
            nullable=False,
        ),
        sa.Column("rule_json", _JSON, nullable=False),
        sa.Column("created_at", _TZDT, nullable=False),
    )
    op.create_table(
        "workspace_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("data", _JSON, nullable=False),
        sa.Column("updated_at", _TZDT, nullable=False),
    )
    op.create_table(
        "members",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("created_at", _TZDT, nullable=False),
    )
    op.create_index("ix_members_subject", "members", ["subject"], unique=True)
    op.create_table(
        "mcp_query_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("client", sa.Text(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", _TZDT, nullable=False),
    )
    op.create_index("ix_mcp_query_log_created_at", "mcp_query_log", ["created_at"])


def downgrade() -> None:
    """Drop everything 0001 created (reverse dependency order)."""
    for table in (
        "mcp_query_log",
        "members",
        "workspace_settings",
        "routing_rules",
        "providers",
        "activity",
        "ingestion_failures",
        "job_events",
        "jobs",
        "secrets",
        "sources",
        "projects",
    ):
        op.drop_table(table)
