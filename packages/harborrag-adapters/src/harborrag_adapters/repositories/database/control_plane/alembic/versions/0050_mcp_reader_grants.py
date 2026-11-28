"""Give the MCP server's Postgres login an explicit allow-list of tables.

A role script used to grant ``harborrag_mcp`` SELECT on every table, now and later,
and then revoke a hand-kept list. From this revision the grants fail closed: the
NOLOGIN role ``harborrag_mcp_read`` holds SELECT on the tables the MCP server reads
and on nothing else, and ``harborrag_mcp`` reads through its membership. Both roles
are created without a password when they are missing; the operator, or
``scripts/deployment/dev.sh mcp-access``, gives ``harborrag_mcp`` its login. Default
privileges that grant tables to either role are removed, so a table a later revision
adds stays closed until a revision grants it. The table list is frozen here on
purpose. The MCP server keeps its own copy in ``control_plane.reader_grants``, and a
test keeps the two equal. SQLite has no roles, so nothing runs there.

The migrating role needs CREATEROLE when a role is missing or when the role setting or
the membership has to change. Only a member of the role that owns a default privilege,
or a superuser, can remove it; one the migrating role cannot remove stops the upgrade
with the clause to run.

Revision ID: 0050
Revises: 0049

Phase: contract
Reversible: yes; the downgrade revokes the allow-list grants and restores nothing
            broader, so the MCP login reads no table until the previous release's
            ``dev.sh mcp-access`` runs its role script again
Touches: role grants on every public table, roles harborrag_mcp and harborrag_mcp_read;
         no rows
Locks: none on table data; each GRANT and REVOKE updates the table's catalog entry, with
       lock_timeout 5s
Deploy: before the application release; the new MCP server refuses to start while its
        login can read a table outside the allow-list
"""

from __future__ import annotations

from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None

_MCP_READER_TABLES = (
    "document_versions",
    "documents",
    "mcp_api_keys",
    "projection_manifests",
    "setting_overrides",
    "setting_revisions",
    "source_scans",
    "source_scopes",
    "summary_bindings",
    "summary_scopes",
    "topology_accepted",
    "topology_assertions",
    "topology_build_documents",
    "topology_build_permissions",
    "topology_derived_artifacts",
    "topology_indexing_configs",
    "topology_mentions",
    "topology_permission_grants",
    "topology_permission_snapshots",
    "topology_policies",
)

_LOCK_TIMEOUT = "SET LOCAL lock_timeout = '5s'"

_CREATE_ROLES = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'harborrag_mcp_read') THEN
        CREATE ROLE harborrag_mcp_read NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'harborrag_mcp') THEN
        CREATE ROLE harborrag_mcp NOLOGIN;
    END IF;
    IF NOT EXISTS (
        SELECT FROM pg_catalog.pg_db_role_setting AS setting
        JOIN pg_catalog.pg_roles AS role ON role.oid = setting.setrole
        WHERE role.rolname = 'harborrag_mcp'
          AND setting.setdatabase = 0
          AND 'default_transaction_read_only=on' = ANY (setting.setconfig)
    ) THEN
        ALTER ROLE harborrag_mcp SET default_transaction_read_only = on;
    END IF;
    IF NOT pg_has_role('harborrag_mcp', 'harborrag_mcp_read', 'MEMBER') THEN
        GRANT harborrag_mcp_read TO harborrag_mcp;
    END IF;
END
$$
"""

_REMOVE_DEFAULT_PRIVILEGES = """
DO $$
DECLARE
    mcp_default_acl CURSOR FOR
        SELECT acl.defaclrole::regrole::text AS creator, space.nspname AS schema_name
        FROM pg_catalog.pg_default_acl AS acl
        LEFT JOIN pg_catalog.pg_namespace AS space ON space.oid = acl.defaclnamespace
        WHERE acl.defaclobjtype = 'r'
          AND EXISTS (
              SELECT FROM aclexplode(acl.defaclacl) AS item
              JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = item.grantee
              WHERE grantee.rolname IN ('harborrag_mcp', 'harborrag_mcp_read')
          );
    remaining text;
BEGIN
    FOR entry IN mcp_default_acl
    LOOP
        BEGIN
            IF entry.schema_name IS NULL THEN
                EXECUTE format(
                    'ALTER DEFAULT PRIVILEGES FOR ROLE %s '
                    'REVOKE ALL ON TABLES FROM harborrag_mcp, harborrag_mcp_read',
                    entry.creator
                );
            ELSE
                EXECUTE format(
                    'ALTER DEFAULT PRIVILEGES FOR ROLE %s IN SCHEMA %I '
                    'REVOKE ALL ON TABLES FROM harborrag_mcp, harborrag_mcp_read',
                    entry.creator, entry.schema_name
                );
            END IF;
        EXCEPTION WHEN insufficient_privilege THEN
            NULL;
        END;
    END LOOP;
    FOR entry IN mcp_default_acl
    LOOP
        remaining := concat_ws(
            '; ',
            remaining,
            'FOR ROLE ' || entry.creator || CASE
                WHEN entry.schema_name IS NULL THEN ''
                ELSE format(' IN SCHEMA %I', entry.schema_name)
            END
        );
    END LOOP;
    IF remaining IS NOT NULL THEN
        RAISE EXCEPTION 'default privileges still grant future tables to the MCP roles: %', remaining
            USING HINT = 'As a superuser or as that role, run ALTER DEFAULT PRIVILEGES <the clause> '
                'REVOKE ALL ON TABLES FROM harborrag_mcp, harborrag_mcp_read; '
                'then apply the migrations again.';
    END IF;
END
$$
"""

_TABLE_ARRAY = "ARRAY[" + ", ".join(f"'{name}'" for name in _MCP_READER_TABLES) + "]::text[]"

_GRANT_TABLES = f"""
DO $$
DECLARE
    table_name text;
BEGIN
    REVOKE ALL ON ALL TABLES IN SCHEMA public FROM harborrag_mcp, harborrag_mcp_read;
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO harborrag_mcp_read', current_database());
    GRANT USAGE ON SCHEMA public TO harborrag_mcp_read;
    FOREACH table_name IN ARRAY {_TABLE_ARRAY}
    LOOP
        IF to_regclass(format('public.%I', table_name)) IS NOT NULL THEN
            EXECUTE format('GRANT SELECT ON TABLE public.%I TO harborrag_mcp_read', table_name);
        END IF;
    END LOOP;
END
$$
"""

_REVOKE_TABLES = """
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'harborrag_mcp_read') THEN
        REVOKE ALL ON ALL TABLES IN SCHEMA public FROM harborrag_mcp_read;
    END IF;
END
$$
"""


def _on_postgres() -> bool:
    """Whether the migration runs against Postgres, online or as emitted SQL."""
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    """Create the MCP roles and grant the reader role exactly the allow-listed tables."""
    if not _on_postgres():
        return
    op.execute(_LOCK_TIMEOUT)
    op.execute(_CREATE_ROLES)
    op.execute(_REMOVE_DEFAULT_PRIVILEGES)
    op.execute(_GRANT_TABLES)


def downgrade() -> None:
    """Revoke the reader role's table grants and restore none of the broader ones."""
    if not _on_postgres():
        return
    op.execute(_LOCK_TIMEOUT)
    op.execute(_REVOKE_TABLES)
