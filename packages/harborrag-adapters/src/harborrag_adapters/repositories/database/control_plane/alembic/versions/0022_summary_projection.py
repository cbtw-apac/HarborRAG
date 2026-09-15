"""Durable summary intent, bindings and immutable tenant generation cache.

Revision ID: 0022
Revises: 0021
"""

from alembic import op
from sqlalchemy import JSON, Column, DateTime, Index, Integer, MetaData, String, Table

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def tables() -> MetaData:
    metadata = MetaData()
    Table(
        "summary_tenants",
        metadata,
        Column("tenant_id", String(128), primary_key=True),
        Column("revision", Integer, nullable=False),
    )
    scopes = Table(
        "summary_scopes",
        metadata,
        Column("tenant_id", String(128), primary_key=True),
        Column("source_scope_id", String(128), primary_key=True),
        Column("revision", Integer, nullable=False),
        Column("fence", Integer, nullable=False),
        Column("policy", JSON(none_as_null=True), nullable=True),
        Column("execution", String(16), nullable=False),
        Column("lease_until", DateTime(timezone=True)),
        Column("dirty_since", DateTime(timezone=True)),
        Column("available_at", DateTime(timezone=True)),
        Column("error_code", String(128)),
    )
    Index("ix_summary_dispatch", scopes.c.tenant_id, scopes.c.execution, scopes.c.available_at)
    bindings = Table(
        "summary_bindings",
        metadata,
        Column("tenant_id", String(128), primary_key=True),
        Column("node_key", String(512), primary_key=True),
        Column("source_scope_id", String(128), nullable=False),
        Column("binding", JSON, nullable=False),
        Column("node", JSON, nullable=False),
    )
    Index("ix_summary_binding_scope", bindings.c.tenant_id, bindings.c.source_scope_id)
    Table(
        "summary_cache",
        metadata,
        Column("tenant_id", String(128), primary_key=True),
        Column("generation_key", String(128), primary_key=True),
        Column("artifact_hash", String(128), nullable=False),
        Column("card", JSON, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
    )
    return metadata


def upgrade() -> None:
    tables().create_all(op.get_bind())


def downgrade() -> None:
    tables().drop_all(op.get_bind())
