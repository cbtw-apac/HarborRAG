"""Frozen ingestion control-plane DDL for revision 0001."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

_JSON = sa.JSON()
_TZDT = sa.DateTime(timezone=True)


def create_tables() -> None:
    op.create_table(
        "source_scopes",
        sa.Column("source_scope_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("connector_type", sa.String(32), nullable=False),
        sa.Column("connection_id", sa.String(255), nullable=False),
        sa.Column("configuration_fingerprint", sa.String(128), nullable=False),
        sa.Column("created_at", _TZDT, nullable=False),
        sa.Column("updated_at", _TZDT, nullable=False),
    )
    op.create_index("ix_source_scopes_tenant_id", "source_scopes", ["tenant_id"])
    op.create_table(
        "source_scans",
        sa.Column("scan_id", sa.String(128), primary_key=True),
        sa.Column(
            "source_scope_id",
            sa.String(128),
            sa.ForeignKey("source_scopes.source_scope_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scan_sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("started_at", _TZDT, nullable=False),
        sa.Column("completed_at", _TZDT, nullable=True),
        sa.Column("discovery_cursor", _JSON, nullable=True),
        sa.Column("seen_count", sa.Integer(), nullable=False),
        sa.Column("failure_reason", sa.String(255), nullable=True),
        sa.UniqueConstraint(
            "source_scope_id",
            "scan_sequence",
            name="uq_source_scan_sequence",
        ),
        sa.CheckConstraint(
            "status IN ('STARTED','COMPLETED','FAILED','CANCELLED')",
            name="ck_source_scan_status",
        ),
    )
    op.create_index(
        "ix_source_scans_scope_status",
        "source_scans",
        ["source_scope_id", "status"],
    )
    op.create_table(
        "source_items",
        sa.Column(
            "source_scope_id",
            sa.String(128),
            sa.ForeignKey("source_scopes.source_scope_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("source_item_id", sa.String(512), primary_key=True),
        sa.Column("document_id", sa.String(128), nullable=False),
        sa.Column("source_version", sa.String(255), nullable=False),
        sa.Column("binding_kind", sa.String(32), nullable=False),
        sa.Column("parent_source_item_id", sa.String(512), nullable=True),
        sa.Column("admission_change_key", sa.String(128), nullable=False),
        sa.Column("last_seen_scan_sequence", sa.Integer(), nullable=False),
        sa.Column("last_reconciled_scan_sequence", sa.Integer(), nullable=True),
        sa.Column("consecutive_misses", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("descriptor", _JSON, nullable=False),
        sa.Column("updated_at", _TZDT, nullable=False),
    )
    op.create_index(
        "ix_source_items_last_seen",
        "source_items",
        ["source_scope_id", "last_seen_scan_sequence"],
    )
    op.create_index("ix_source_items_document_id", "source_items", ["document_id"])
    op.create_table(
        "documents",
        sa.Column("document_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("source_scope_id", sa.String(128), nullable=False),
        sa.Column("connector_type", sa.String(32), nullable=False),
        sa.Column("connection_id", sa.String(255), nullable=False),
        sa.Column("source_item_id", sa.String(512), nullable=False),
        sa.Column("active_document_version_id", sa.String(128), nullable=True),
        sa.Column("created_at", _TZDT, nullable=False),
        sa.Column("updated_at", _TZDT, nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "connector_type",
            "connection_id",
            "source_item_id",
            name="uq_document_source_identity",
        ),
    )
    op.create_index("ix_documents_tenant_id", "documents", ["tenant_id"])
    op.create_index("ix_documents_source_scope_id", "documents", ["source_scope_id"])
    _create_document_versions()
    _create_task_tables()
    _create_reliability_tables()


def _create_document_versions() -> None:
    op.create_table(
        "document_versions",
        sa.Column("document_version_id", sa.String(128), primary_key=True),
        sa.Column(
            "document_id",
            sa.String(128),
            sa.ForeignKey("documents.document_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("canonical_content_hash", sa.String(128), nullable=False),
        sa.Column("retrieval_metadata_hash", sa.String(128), nullable=False),
        sa.Column("processing_fingerprint", sa.String(128), nullable=False),
        sa.Column("admission_change_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("raw_artifact", _JSON, nullable=True),
        sa.Column("raw_metadata_artifact", _JSON, nullable=True),
        sa.Column("canonical_artifact", _JSON, nullable=True),
        sa.Column("chunk_artifact", _JSON, nullable=True),
        sa.Column("chunk_index_artifact", _JSON, nullable=True),
        sa.Column("relation_artifact", _JSON, nullable=True),
        sa.Column("representation_artifact", _JSON, nullable=True),
        sa.Column("created_at", _TZDT, nullable=False),
        sa.Column("updated_at", _TZDT, nullable=False),
        sa.Column("activated_at", _TZDT, nullable=True),
        sa.Column("retired_at", _TZDT, nullable=True),
        sa.CheckConstraint(
            "status IN ("
            "'PENDING','RAW_CAPTURED','CANONICAL_READY','CHUNKS_READY',"
            "'REPRESENTATIONS_READY','PROJECTIONS_STAGED','VERIFIED',"
            "'ACTIVE','RETIRED','FAILED'"
            ")",
            name="ck_document_version_status",
        ),
    )
    op.create_index(
        "uq_document_one_active_version",
        "document_versions",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
        sqlite_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_index(
        "ix_document_versions_document_status",
        "document_versions",
        ["document_id", "status"],
    )


def _create_task_tables() -> None:
    op.create_table(
        "ingestion_tasks",
        sa.Column("task_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("source_scope_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("submitted_at", _TZDT, nullable=False),
        sa.Column("started_at", _TZDT, nullable=True),
        sa.Column("completed_at", _TZDT, nullable=True),
        sa.Column("request", _JSON, nullable=False),
        sa.Column("summary", _JSON, nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=True),
        sa.Column("request_hash", sa.String(64), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','COMPLETED','PARTIAL','FAILED','CANCELLED')",
            name="ck_ingestion_task_status",
        ),
        sa.CheckConstraint(
            "(idempotency_key IS NULL AND request_hash IS NULL) OR "
            "(idempotency_key IS NOT NULL AND request_hash IS NOT NULL)",
            name="ck_ingestion_task_idempotency_pair",
        ),
    )
    op.create_index("ix_ingestion_tasks_tenant_id", "ingestion_tasks", ["tenant_id"])
    op.create_index(
        "ix_ingestion_tasks_source_scope_id",
        "ingestion_tasks",
        ["source_scope_id"],
    )
    op.create_index(
        "uq_ingestion_task_idempotency_key",
        "ingestion_tasks",
        ["tenant_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
        sqlite_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_table(
        "task_document_results",
        sa.Column(
            "task_id",
            sa.String(128),
            sa.ForeignKey("ingestion_tasks.task_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("document_id", sa.String(128), primary_key=True),
        sa.Column("document_version_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("result", _JSON, nullable=False),
        sa.Column("completed_at", _TZDT, nullable=False),
    )


def _create_reliability_tables() -> None:
    op.create_table(
        "document_failures",
        sa.Column("failure_id", sa.String(128), primary_key=True),
        sa.Column("document_id", sa.String(128), nullable=False),
        sa.Column("document_version_id", sa.String(128), nullable=False),
        sa.Column("failed_stage", sa.String(64), nullable=False),
        sa.Column("failure_category", sa.String(64), nullable=False),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("safe_error_code", sa.String(128), nullable=False),
        sa.Column("artifact_references", _JSON, nullable=False),
        sa.Column("projection_manifest_reference", _JSON, nullable=True),
        sa.Column("first_failure_at", _TZDT, nullable=False),
        sa.Column("last_failure_at", _TZDT, nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "document_version_id",
            "failed_stage",
            "safe_error_code",
            name="uq_document_failure_identity",
        ),
    )
    op.create_index("ix_document_failures_document_id", "document_failures", ["document_id"])
    op.create_index(
        "ix_document_failures_document_version_id",
        "document_failures",
        ["document_version_id"],
    )
    op.create_table(
        "projection_manifests",
        sa.Column("document_version_id", sa.String(128), primary_key=True),
        sa.Column("document_id", sa.String(128), nullable=False),
        sa.Column("manifest", _JSON, nullable=False),
        sa.Column("verified_at", _TZDT, nullable=True),
        sa.Column("created_at", _TZDT, nullable=False),
        sa.Column("updated_at", _TZDT, nullable=False),
    )
    op.create_index(
        "ix_projection_manifests_document_id",
        "projection_manifests",
        ["document_id"],
    )
    op.create_table(
        "projection_cleanup_jobs",
        sa.Column("cleanup_job_id", sa.String(128), primary_key=True),
        sa.Column("document_id", sa.String(128), nullable=False),
        sa.Column("document_version_id", sa.String(128), nullable=False, unique=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error_code", sa.String(128), nullable=True),
        sa.Column("created_at", _TZDT, nullable=False),
        sa.Column("updated_at", _TZDT, nullable=False),
        sa.Column("completed_at", _TZDT, nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','COMPLETED','FAILED','CANCELLED')",
            name="ck_projection_cleanup_status",
        ),
    )
    op.create_index(
        "ix_projection_cleanup_jobs_document_id",
        "projection_cleanup_jobs",
        ["document_id"],
    )
    _create_reindex_jobs()


def _create_reindex_jobs() -> None:
    op.create_table(
        "reindex_jobs",
        sa.Column("reindex_job_id", sa.String(128), primary_key=True),
        sa.Column("document_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("target_processing_fingerprint", sa.String(128), nullable=False),
        sa.Column("connector_call_count", sa.Integer(), nullable=False),
        sa.Column("scanned_count", sa.Integer(), nullable=False),
        sa.Column("processed_count", sa.Integer(), nullable=False),
        sa.Column("published_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("last_error_code", sa.String(128), nullable=True),
        sa.Column("created_at", _TZDT, nullable=False),
        sa.Column("updated_at", _TZDT, nullable=False),
        sa.Column("completed_at", _TZDT, nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','COMPLETED','FAILED','CANCELLED')",
            name="ck_reindex_job_status",
        ),
        sa.CheckConstraint(
            "connector_call_count = 0",
            name="ck_reindex_connector_calls_zero",
        ),
    )
    op.create_index("ix_reindex_jobs_document_id", "reindex_jobs", ["document_id"])


def drop_tables() -> None:
    for table in (
        "reindex_jobs",
        "projection_cleanup_jobs",
        "projection_manifests",
        "document_failures",
        "task_document_results",
        "ingestion_tasks",
        "document_versions",
        "documents",
        "source_items",
        "source_scans",
        "source_scopes",
    ):
        op.drop_table(table)
