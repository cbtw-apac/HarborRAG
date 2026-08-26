from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)

from harborrag_adapters.repositories.backends.sqlalchemy import UTCDateTime
from harborrag_core.ingestion import (
    CleanupJobState,
    DocumentVersionState,
    IngestionTaskState,
    ReindexJobState,
    SourceScanState,
)

METADATA = MetaData()

SOURCE_SCOPES = Table(
    "source_scopes",
    METADATA,
    Column("source_scope_id", String(128), primary_key=True),
    Column("tenant_id", String(128), nullable=False, index=True),
    Column("connector_type", String(32), nullable=False),
    Column("connection_id", String(255), nullable=False),
    Column("configuration_fingerprint", String(128), nullable=False),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("updated_at", UTCDateTime(), nullable=False),
)

SOURCE_SCANS = Table(
    "source_scans",
    METADATA,
    Column("scan_id", String(128), primary_key=True),
    Column(
        "source_scope_id",
        String(128),
        ForeignKey("source_scopes.source_scope_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("scan_sequence", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("started_at", UTCDateTime(), nullable=False),
    Column("completed_at", UTCDateTime(), nullable=True),
    Column("discovery_cursor", JSON, nullable=True),
    Column("seen_count", Integer, nullable=False, default=0),
    Column("failure_reason", String(255), nullable=True),
    UniqueConstraint("source_scope_id", "scan_sequence", name="uq_source_scan_sequence"),
    CheckConstraint(
        f"status IN ({','.join(repr(state.value) for state in SourceScanState)})",
        name="ck_source_scan_status",
    ),
)
Index("ix_source_scans_scope_status", SOURCE_SCANS.c.source_scope_id, SOURCE_SCANS.c.status)

SOURCE_ITEMS = Table(
    "source_items",
    METADATA,
    Column(
        "source_scope_id",
        String(128),
        ForeignKey("source_scopes.source_scope_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("source_item_id", String(512), primary_key=True),
    Column("document_id", String(128), nullable=False),
    Column("source_version", String(255), nullable=False),
    Column("binding_kind", String(32), nullable=False),
    Column("parent_source_item_id", String(512), nullable=True),
    Column("admission_change_key", String(128), nullable=False),
    Column("last_seen_scan_sequence", Integer, nullable=False),
    Column("last_reconciled_scan_sequence", Integer, nullable=True),
    Column("consecutive_misses", Integer, nullable=False, default=0),
    Column("is_active", Boolean, nullable=False, default=True),
    Column("descriptor", JSON, nullable=False, default=dict),
    Column("updated_at", UTCDateTime(), nullable=False),
)
Index(
    "ix_source_items_last_seen",
    SOURCE_ITEMS.c.source_scope_id,
    SOURCE_ITEMS.c.last_seen_scan_sequence,
)
Index("ix_source_items_document_id", SOURCE_ITEMS.c.document_id)

DOCUMENTS = Table(
    "documents",
    METADATA,
    Column("document_id", String(128), primary_key=True),
    Column("tenant_id", String(128), nullable=False, index=True),
    Column("source_scope_id", String(128), nullable=False, index=True),
    Column("connector_type", String(32), nullable=False),
    Column("connection_id", String(255), nullable=False),
    Column("source_item_id", String(512), nullable=False),
    Column("active_document_version_id", String(128), nullable=True),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("updated_at", UTCDateTime(), nullable=False),
    UniqueConstraint(
        "tenant_id",
        "connector_type",
        "connection_id",
        "source_item_id",
        name="uq_document_source_identity",
    ),
)

DOCUMENT_VERSIONS = Table(
    "document_versions",
    METADATA,
    Column("document_version_id", String(128), primary_key=True),
    Column(
        "document_id",
        String(128),
        ForeignKey("documents.document_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("canonical_content_hash", String(128), nullable=False),
    Column("retrieval_metadata_hash", String(128), nullable=False),
    Column("processing_fingerprint", String(128), nullable=False),
    Column("admission_change_key", String(128), nullable=False),
    Column("status", String(32), nullable=False),
    Column("raw_artifact", JSON, nullable=True),
    Column("raw_metadata_artifact", JSON, nullable=True),
    Column("canonical_artifact", JSON, nullable=True),
    Column("chunk_artifact", JSON, nullable=True),
    Column("chunk_index_artifact", JSON, nullable=True),
    Column("relation_artifact", JSON, nullable=True),
    Column("representation_artifact", JSON, nullable=True),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("updated_at", UTCDateTime(), nullable=False),
    Column("activated_at", UTCDateTime(), nullable=True),
    Column("retired_at", UTCDateTime(), nullable=True),
    CheckConstraint(
        f"status IN ({','.join(repr(state.value) for state in DocumentVersionState)})",
        name="ck_document_version_status",
    ),
)
Index(
    "uq_document_one_active_version",
    DOCUMENT_VERSIONS.c.document_id,
    unique=True,
    postgresql_where=text("status = 'ACTIVE'"),
    sqlite_where=text("status = 'ACTIVE'"),
)
Index(
    "ix_document_versions_document_status",
    DOCUMENT_VERSIONS.c.document_id,
    DOCUMENT_VERSIONS.c.status,
)

INGESTION_TASKS = Table(
    "ingestion_tasks",
    METADATA,
    Column("task_id", String(128), primary_key=True),
    Column("tenant_id", String(128), nullable=False, index=True),
    Column("source_scope_id", String(128), nullable=False, index=True),
    Column("status", String(32), nullable=False),
    Column("submitted_at", UTCDateTime(), nullable=False),
    Column("started_at", UTCDateTime(), nullable=True),
    Column("completed_at", UTCDateTime(), nullable=True),
    Column("request", JSON, nullable=False),
    Column("summary", JSON, nullable=False, default=dict),
    Column("idempotency_key", String(255), nullable=True),
    Column("request_hash", String(64), nullable=True),
    Column("event_sequence", Integer, nullable=False, server_default="0"),
    CheckConstraint(
        f"status IN ({','.join(repr(state.value) for state in IngestionTaskState)})",
        name="ck_ingestion_task_status",
    ),
    CheckConstraint(
        "(idempotency_key IS NULL AND request_hash IS NULL) OR "
        "(idempotency_key IS NOT NULL AND request_hash IS NOT NULL)",
        name="ck_ingestion_task_idempotency_pair",
    ),
)
# Serves the public task list: filter by tenant, then walk
# (submitted_at, task_id) descending. Without it every page sorts the tenant's
# whole task history instead of scanning the keyset.
Index(
    "ix_ingestion_tasks_tenant_submitted",
    INGESTION_TASKS.c.tenant_id,
    INGESTION_TASKS.c.submitted_at.desc(),
    INGESTION_TASKS.c.task_id.desc(),
)
Index(
    "uq_ingestion_task_idempotency_key",
    INGESTION_TASKS.c.tenant_id,
    INGESTION_TASKS.c.idempotency_key,
    unique=True,
    postgresql_where=INGESTION_TASKS.c.idempotency_key.is_not(None),
    sqlite_where=INGESTION_TASKS.c.idempotency_key.is_not(None),
)

TASK_EVENTS = Table(
    "task_events",
    METADATA,
    Column(
        "task_id",
        String(128),
        ForeignKey("ingestion_tasks.task_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("seq", Integer, primary_key=True),
    Column("name", Text, nullable=False),
    Column("trace_id", Text, nullable=False),
    Column("payload_json", JSON, nullable=False),
    Column("created_at", UTCDateTime(), nullable=False),
)

TASK_DOCUMENT_RESULTS = Table(
    "task_document_results",
    METADATA,
    Column(
        "task_id",
        String(128),
        ForeignKey("ingestion_tasks.task_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("document_id", String(128), primary_key=True),
    Column("document_version_id", String(128), nullable=True),
    Column("status", String(32), nullable=False),
    Column("result", JSON, nullable=False, default=dict),
    Column("completed_at", UTCDateTime(), nullable=False),
)

DOCUMENT_FAILURES = Table(
    "document_failures",
    METADATA,
    Column("failure_id", String(128), primary_key=True),
    Column("document_id", String(128), nullable=False, index=True),
    Column("document_version_id", String(128), nullable=False, index=True),
    Column("failed_stage", String(64), nullable=False),
    Column("failure_category", String(64), nullable=False),
    Column("retryable", Boolean, nullable=False),
    Column("safe_error_code", String(128), nullable=False),
    Column("artifact_references", JSON, nullable=False, default=list),
    Column("projection_manifest_reference", JSON, nullable=True),
    Column("first_failure_at", UTCDateTime(), nullable=False),
    Column("last_failure_at", UTCDateTime(), nullable=False),
    Column("attempt_count", Integer, nullable=False, default=1),
    UniqueConstraint(
        "document_version_id",
        "failed_stage",
        "safe_error_code",
        name="uq_document_failure_identity",
    ),
)

PROJECTION_MANIFESTS = Table(
    "projection_manifests",
    METADATA,
    Column("document_version_id", String(128), primary_key=True),
    Column("document_id", String(128), nullable=False, index=True),
    Column("manifest", JSON, nullable=False),
    Column("verified_at", UTCDateTime(), nullable=True),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("updated_at", UTCDateTime(), nullable=False),
)

PROJECTION_CLEANUP_JOBS = Table(
    "projection_cleanup_jobs",
    METADATA,
    Column("cleanup_job_id", String(128), primary_key=True),
    Column("document_id", String(128), nullable=False, index=True),
    Column("document_version_id", String(128), nullable=False, unique=True),
    Column("status", String(32), nullable=False),
    Column("attempt_count", Integer, nullable=False, default=0),
    Column("last_error_code", String(128), nullable=True),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("updated_at", UTCDateTime(), nullable=False),
    Column("completed_at", UTCDateTime(), nullable=True),
    CheckConstraint(
        f"status IN ({','.join(repr(state.value) for state in CleanupJobState)})",
        name="ck_projection_cleanup_status",
    ),
)

REINDEX_JOBS = Table(
    "reindex_jobs",
    METADATA,
    Column("reindex_job_id", String(128), primary_key=True),
    Column("document_id", String(128), nullable=True, index=True),
    Column("status", String(32), nullable=False),
    Column("target_processing_fingerprint", String(128), nullable=False),
    Column("connector_call_count", Integer, nullable=False, default=0),
    Column("scanned_count", Integer, nullable=False, default=0),
    Column("processed_count", Integer, nullable=False, default=0),
    Column("published_count", Integer, nullable=False, default=0),
    Column("skipped_count", Integer, nullable=False, default=0),
    Column("failure_count", Integer, nullable=False, default=0),
    Column("last_error_code", String(128), nullable=True),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("updated_at", UTCDateTime(), nullable=False),
    Column("completed_at", UTCDateTime(), nullable=True),
    CheckConstraint(
        f"status IN ({','.join(repr(state.value) for state in ReindexJobState)})",
        name="ck_reindex_job_status",
    ),
    CheckConstraint(
        "connector_call_count = 0",
        name="ck_reindex_connector_calls_zero",
    ),
)
