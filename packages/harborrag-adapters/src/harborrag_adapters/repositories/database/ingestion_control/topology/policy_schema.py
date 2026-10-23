"""Canonical mode epochs, resolved permissions, and shared tenant spending ledger."""

from sqlalchemy import JSON, BigInteger, Boolean, Column, Index, Integer, String, Table

from harborrag_adapters.repositories.backends.sqlalchemy import UTCDateTime

from ..schema import METADATA

INDEXING_CONFIGS = Table(
    "topology_indexing_configs",
    METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("epoch", Integer, nullable=False),
    Column("enabled", Boolean, nullable=False),
    Column("prohibited", Boolean, nullable=False),
    Column("spending_paused", Boolean, nullable=False),
    Column("config", JSON, nullable=False),
)
PERMISSION_SNAPSHOTS = Table(
    "topology_permission_snapshots",
    METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("resource_kind", String(32), primary_key=True),
    Column("resource_id", String(128), primary_key=True),
    Column("revision", String(128), nullable=False),
    Column("known", Boolean, nullable=False),
    Column("processing_allowed", Boolean, nullable=False),
    Column("public", Boolean, nullable=False),
    Column("resolved_at", UTCDateTime(), nullable=False),
    Column("expires_at", UTCDateTime(), nullable=False),
    Column("snapshot", JSON, nullable=False),
)
PERMISSION_GRANTS = Table(
    "topology_permission_grants",
    METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("resource_kind", String(32), primary_key=True),
    Column("resource_id", String(128), primary_key=True),
    Column("principal_id", String(255), primary_key=True),
    Column("allowed", Boolean, nullable=False),
    Column("denied", Boolean, nullable=False),
)
PERMISSION_HISTORY = Table(
    "topology_permission_history",
    METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("resource_kind", String(32), primary_key=True),
    Column("resource_id", String(128), primary_key=True),
    Column("revision", String(128), primary_key=True),
    Column("snapshot", JSON, nullable=False),
)
BUILD_PERMISSIONS = Table(
    "topology_build_permissions",
    METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("build_id", String(128), primary_key=True),
    Column("resource_kind", String(32), primary_key=True),
    Column("resource_id", String(128), primary_key=True),
    Column("revision", String(128), nullable=False),
)
BUILD_DOCUMENTS = Table(
    "topology_build_documents",
    METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("build_id", String(128), primary_key=True),
    Column("document_id", String(128), primary_key=True),
    Column("document_version_id", String(128), nullable=False),
)
BUDGET_DAYS = Table(
    "topology_budget_days",
    METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("day", String(10), primary_key=True),
    Column("tokens", BigInteger, nullable=False),
    Column("cost_microusd", BigInteger, nullable=False),
)
BUDGET_RESERVATIONS = Table(
    "topology_budget_reservations",
    METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("reservation_id", String(128), primary_key=True),
    Column("job_id", String(128), nullable=False),
    Column("fence", Integer, nullable=False),
    Column("day", String(10), nullable=False),
    Column("state", String(32), nullable=False),
    Column("expires_at", UTCDateTime(), nullable=False),
    Column("tokens", BigInteger, nullable=False),
    Column("cost_microusd", BigInteger, nullable=False),
    Column("operation_key", String(128), nullable=True),
    Column("provider_calls", Integer, nullable=False),
    Column("reservation", JSON, nullable=False),
    Column("request", JSON, nullable=False),
    Column("settlement", JSON, nullable=True),
)
DERIVED_ARTIFACTS = Table(
    "topology_derived_artifacts",
    METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("artifact_id", String(128), primary_key=True),
    Column("build_id", String(128), nullable=False),
    Column("artifact_kind", String(32), nullable=False),
    Column("lineage", JSON, nullable=False),
    Column("artifact", JSON, nullable=False),
)

Index(
    "ix_topology_budget_live",
    BUDGET_RESERVATIONS.c.tenant_id,
    BUDGET_RESERVATIONS.c.state,
    BUDGET_RESERVATIONS.c.expires_at,
)
Index(
    "ix_topology_budget_operation",
    BUDGET_RESERVATIONS.c.tenant_id,
    BUDGET_RESERVATIONS.c.operation_key,
    BUDGET_RESERVATIONS.c.job_id,
)
Index(
    "ix_topology_derived_build",
    DERIVED_ARTIFACTS.c.tenant_id,
    DERIVED_ARTIFACTS.c.build_id,
    DERIVED_ARTIFACTS.c.artifact_kind,
)
