"""Add the MCP API-key table shared by every MCP replica.

``mcp_api_keys`` holds the SHA-256 hash and lifecycle columns, so every replica sees the same
keys and a leaked table exposes nothing usable. The hex-format checks use
PostgreSQL's ``~`` operator and are skipped on SQLite.

Revision ID: 0036
Revises: 0035
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

_TABLE = "mcp_api_keys"
_IX_TENANT_OWNER = "ix_mcp_api_keys_tenant_owner"
_TZDT = sa.DateTime(timezone=True)
_NO_DELETE_FN = "mcp_api_keys_no_delete"
_NO_DELETE_TRIGGER = "trg_mcp_api_keys_no_delete"


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    checks = [
        sa.CheckConstraint("environment IN ('dev','staging','prod')", name="ck_mcp_key_env"),
        sa.CheckConstraint("expires_at > created_at", name="ck_mcp_key_expiry"),
    ]
    if _is_postgresql():
        checks += [
            sa.CheckConstraint("key_id ~ '^[0-9a-f]{24}$'", name="ck_mcp_key_id"),
            sa.CheckConstraint("secret_hash ~ '^[0-9a-f]{64}$'", name="ck_mcp_key_hash"),
        ]
    op.create_table(
        _TABLE,
        sa.Column("key_id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("owner", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column("environment", sa.String(length=16), nullable=False),
        sa.Column("created_at", _TZDT, nullable=False, server_default=sa.func.now()),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("expires_at", _TZDT, nullable=False),
        sa.Column("revoked_at", _TZDT, nullable=True),
        sa.Column("revoked_by", sa.Text(), nullable=True),
        sa.Column("revocation_reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("key_id", name=op.f("pk_mcp_api_keys")),
        *checks,
    )
    op.create_index(_IX_TENANT_OWNER, _TABLE, ["tenant_id", "owner"])

    # Rows are never deleted -- revoke instead. The repository has no delete
    # path; this trigger makes the database refuse one too (PostgreSQL only).
    if _is_postgresql():
        op.execute(
            f"""
            CREATE FUNCTION {_NO_DELETE_FN}() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION '{_TABLE} rows are never deleted; revoke instead';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {_NO_DELETE_TRIGGER} BEFORE DELETE ON {_TABLE}
            FOR EACH ROW EXECUTE FUNCTION {_NO_DELETE_FN}()
            """
        )


def downgrade() -> None:
    if _is_postgresql():
        op.execute(f"DROP TRIGGER IF EXISTS {_NO_DELETE_TRIGGER} ON {_TABLE}")
        op.execute(f"DROP FUNCTION IF EXISTS {_NO_DELETE_FN}()")
    op.drop_index(_IX_TENANT_OWNER, table_name=_TABLE)
    op.drop_table(_TABLE)
