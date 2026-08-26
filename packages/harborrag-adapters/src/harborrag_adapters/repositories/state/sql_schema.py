from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)

from harborrag_adapters.repositories.backends.sqlalchemy import (
    UTCDateTime,
)

_METADATA = MetaData()

_WORKFLOW_STATE = Table(
    "harbor_workflow_state",
    _METADATA,
    Column("tenant_id", String(64), primary_key=True),
    Column("workflow_id", String(64), primary_key=True),
    Column("status", String(32), nullable=False),
    Column("current_step", String(255), nullable=True),
    Column("payload", JSON, nullable=False, default=dict),
    Column("cursor", JSON, nullable=False, default=dict),
    Column("retry_count", Integer, nullable=False, default=0),
    Column("version", Integer, nullable=False, default=1),
    Column("cancellation_requested", Boolean, nullable=False, default=False),
    Column("error", JSON, nullable=True),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("updated_at", UTCDateTime(), nullable=False),
    Column("expires_at", UTCDateTime(), nullable=True),
)

_CHECKPOINTS = Table(
    "harbor_checkpoints",
    _METADATA,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False, index=True),
    Column("workflow_id", String(64), nullable=False, index=True),
    Column("step_name", String(255), nullable=False),
    Column("cursor", JSON, nullable=False, default=dict),
    Column("payload", JSON, nullable=False, default=dict),
    Column("state_version", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("parent_checkpoint_id", String(64), nullable=True),
    Column("created_at", UTCDateTime(), nullable=False),
    UniqueConstraint(
        "tenant_id",
        "workflow_id",
        "state_version",
        name="uq_harbor_checkpoint_stream_version",
    ),
)

_LEASES = Table(
    "harbor_leases",
    _METADATA,
    Column("tenant_id", String(64), primary_key=True),
    Column("resource", String(255), primary_key=True),
    Column("owner_token", String(64), nullable=False),
    Column("fencing_token", Integer, nullable=False),
    Column("acquired_at", UTCDateTime(), nullable=False),
    Column("expires_at", UTCDateTime(), nullable=False),
)

_LEASE_FENCING = Table(
    "harbor_lease_fencing",
    _METADATA,
    Column("tenant_id", String(64), primary_key=True),
    Column("resource", String(255), primary_key=True),
    Column("counter", Integer, nullable=False, default=0),
)
