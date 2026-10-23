"""Topology tables share the ingestion authority transaction and metadata."""

from sqlalchemy import JSON, Boolean, Column, Index, Integer, String, Table

from harborrag_adapters.repositories.backends.sqlalchemy import UTCDateTime

from ..schema import METADATA

TOPOLOGY_POLICIES = Table(
    "topology_policies",
    METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("source_scope_id", String(128), primary_key=True),
    Column("enabled", Boolean, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("fingerprint", String(128), nullable=False),
    Column("policy", JSON, nullable=False),
)
TOPOLOGY_JOBS = Table(
    "topology_jobs",
    METADATA,
    Column("job_id", String(128), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("source_scope_id", String(128), nullable=False),
    Column("document_id", String(128), nullable=False),
    Column("document_version_id", String(128), nullable=False),
    Column("policy_revision", Integer, nullable=False),
    Column("fingerprint", String(128), nullable=False),
    Column("policy", JSON, nullable=False),
    Column("state", String(32), nullable=False),
    Column("fence", Integer, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("lease_until", UTCDateTime(), nullable=True),
    Column("error_code", String(128), nullable=True),
    Column("expected_chunks", JSON, nullable=True),
    Column("config_epoch", Integer, nullable=False, default=0),
    Column("permission_dependencies", JSON, nullable=False, default=list),
    Column("source_permission_revision", String(128), nullable=False, default=""),
    Column("document_permission_revision", String(128), nullable=False, default=""),
    Column("available_at", UTCDateTime(), nullable=True),
    Column("created_at", UTCDateTime(), nullable=False),
)
Index("ix_topology_jobs_dispatch", TOPOLOGY_JOBS.c.tenant_id, TOPOLOGY_JOBS.c.state)
TOPOLOGY_CHECKPOINTS = Table(
    "topology_checkpoints",
    METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("job_id", String(128), primary_key=True),
    Column("chunk_id", String(256), primary_key=True),
    Column("input_digest", String(128), nullable=False),
    Column("extraction_fingerprint", String(128), nullable=False),
    Column("checkpoint", JSON, nullable=False),
)
Index(
    "ix_topology_checkpoint_reuse",
    TOPOLOGY_CHECKPOINTS.c.tenant_id,
    TOPOLOGY_CHECKPOINTS.c.extraction_fingerprint,
    TOPOLOGY_CHECKPOINTS.c.input_digest,
)
TOPOLOGY_BUILDS = Table(
    "topology_builds",
    METADATA,
    Column("build_id", String(128), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("job_id", String(128), nullable=False),
    Column("fence", Integer, nullable=False),
    Column("verified", Boolean, nullable=False, default=False),
    Column("manifest", JSON, nullable=False),
)
TOPOLOGY_ACCEPTED = Table(
    "topology_accepted",
    METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("document_id", String(128), primary_key=True),
    Column("document_version_id", String(128), nullable=False),
    Column("source_scope_id", String(128), nullable=False),
    Column("policy_revision", Integer, nullable=False),
    Column("build_id", String(128), nullable=False),
    Column("config_epoch", Integer, nullable=False, default=0),
)
TOPOLOGY_MENTIONS = Table(
    "topology_mentions",
    METADATA,
    Column("mention_id", String(128), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("build_id", String(128), nullable=False),
    Column("entity_id", String(128), nullable=False),
    Column("chunk_id", String(256), nullable=False),
    Column("label_key", String(256), nullable=False),
    Column("record", JSON, nullable=False),
)
Index("ix_topology_mentions_label", TOPOLOGY_MENTIONS.c.tenant_id, TOPOLOGY_MENTIONS.c.label_key)
Index("ix_topology_mentions_build", TOPOLOGY_MENTIONS.c.build_id)
TOPOLOGY_ASSERTIONS = Table(
    "topology_assertions",
    METADATA,
    Column("assertion_id", String(128), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("build_id", String(128), nullable=False),
    Column("subject_entity_id", String(128), nullable=False),
    Column("object_entity_id", String(128), nullable=False),
    Column("record", JSON, nullable=False),
)
Index("ix_topology_assertions_build", TOPOLOGY_ASSERTIONS.c.build_id)

TOPOLOGY_RESOLUTION_HEADS = Table(
    "topology_resolution_heads",
    METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("revision", Integer, nullable=False),
)
TOPOLOGY_RESOLUTION_DECISIONS = Table(
    "topology_resolution_decisions",
    METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("decision_id", String(128), primary_key=True),
    Column("revision", Integer, nullable=False),
    Column("decision", JSON, nullable=False),
)
TOPOLOGY_RESOLUTION_SNAPSHOTS = Table(
    "topology_resolution_snapshots",
    METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("resolution_revision", String(128), primary_key=True),
    Column("mapping", JSON, nullable=False),
    Column("proofs", JSON, nullable=False, default=dict),
)
