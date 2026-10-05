"""MCP API-key ORM row.

Kept in its own module (file-length gate) and imported from schemas.py so it
registers on the shared ``Base`` metadata -- required for Alembic
autogenerate and the metadata-drift test to see the table.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import conv

from harborrag_adapters.repositories.backends.sqlalchemy import UTCDateTime

from .schemas import Base


class McpApiKeyRow(Base):
    __tablename__ = "mcp_api_keys"
    __table_args__ = (
        # conv() keeps exact names; `~` is PostgreSQL-only, SQLite skips the hex checks.
        sa.CheckConstraint("key_id ~ '^[0-9a-f]{24}$'", name=conv("ck_mcp_key_id")).ddl_if(
            dialect="postgresql"
        ),
        sa.CheckConstraint("secret_hash ~ '^[0-9a-f]{64}$'", name=conv("ck_mcp_key_hash")).ddl_if(
            dialect="postgresql"
        ),
        sa.CheckConstraint("environment IN ('dev','staging','prod')", name=conv("ck_mcp_key_env")),
        sa.CheckConstraint("expires_at > created_at", name=conv("ck_mcp_key_expiry")),
        sa.Index("ix_mcp_api_keys_tenant_owner", "tenant_id", "owner"),
    )

    key_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    owner: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    secret_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    environment: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=sa.func.now()
    )
    created_by: Mapped[str] = mapped_column(sa.Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    revocation_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
