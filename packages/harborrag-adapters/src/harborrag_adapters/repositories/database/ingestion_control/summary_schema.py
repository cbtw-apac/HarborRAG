"""Summary authority; graph properties and vector records are disposable views."""

from sqlalchemy import JSON, Column, Index, Integer, String, Table

from harborrag_adapters.repositories.backends.sqlalchemy import UTCDateTime

from .schema import METADATA

SUMMARY_TENANTS = Table(
    "summary_tenants",
    METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("revision", Integer, nullable=False, default=0),
)
SUMMARY_SCOPES = Table(
    "summary_scopes",
    METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("source_scope_id", String(128), primary_key=True),
    Column("revision", Integer, nullable=False),
    Column("fence", Integer, nullable=False),
    Column("policy", JSON(none_as_null=True), nullable=True),
    Column("execution", String(16), nullable=False),
    Column("lease_until", UTCDateTime(), nullable=True),
    Column("dirty_since", UTCDateTime(), nullable=True),
    Column("available_at", UTCDateTime(), nullable=True),
    Column("error_code", String(128), nullable=True),
)
Index(
    "ix_summary_dispatch",
    SUMMARY_SCOPES.c.tenant_id,
    SUMMARY_SCOPES.c.execution,
    SUMMARY_SCOPES.c.available_at,
)
SUMMARY_BINDINGS = Table(
    "summary_bindings",
    METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("node_key", String(512), primary_key=True),
    Column("source_scope_id", String(128), nullable=False),
    Column("binding", JSON, nullable=False),
    Column("node", JSON, nullable=False),
)
Index("ix_summary_binding_scope", SUMMARY_BINDINGS.c.tenant_id, SUMMARY_BINDINGS.c.source_scope_id)
SUMMARY_CACHE = Table(
    "summary_cache",
    METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("generation_key", String(128), primary_key=True),
    Column("artifact_hash", String(128), nullable=False),
    Column("card", JSON, nullable=False),
    Column("created_at", UTCDateTime(), nullable=False),
)
