"""Tables the MCP server's Postgres login may read, and the query that checks its grants.

``MCP_READER_TABLES`` is the exact set of tables the eleven reader tools, API key
verification, the settings provider and the tenant directory query. Migrations never
import it: a revision that changes the MCP grants lists its own tables, and a test
keeps that list equal to this one. A table joins the set only with a revision that
grants it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from harborrag_core.contracts.errors import HarborUnavailableError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

MCP_READER_LOGIN = "harborrag_mcp"
"""The MCP server's Postgres login; the migrations create it without a password."""

MCP_READER_TABLES: frozenset[str] = frozenset(
    {
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
    }
)
"""Public tables the MCP login may read; every other table stays closed to it."""

_READABLE_PUBLIC_TABLES = sa.text(
    """
    SELECT c.relname
    FROM pg_catalog.pg_class AS c
    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
    WHERE current_user = :login
      AND n.nspname = 'public'
      AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND has_any_column_privilege(c.oid, 'SELECT')
    ORDER BY c.relname
    """
)


async def tables_beyond_reader_grants(engine: AsyncEngine) -> tuple[str, ...]:
    """List the public tables the MCP login can read outside ``MCP_READER_TABLES``.

    A grant counts whether it names the login, a role the login inherits from or
    PUBLIC, and whether it covers the table or one of its columns. Any other login
    gets an empty answer, because only the MCP login is held to the allow-list.

    Args:
        engine: Postgres engine that connects as the login to check.

    Returns:
        The table names in alphabetical order; empty when none is outside the allow-list.

    Raises:
        HarborUnavailableError: The database cannot be reached or read.
    """
    statement = _READABLE_PUBLIC_TABLES.bindparams(login=MCP_READER_LOGIN)
    try:
        async with engine.connect() as connection:
            readable = list(await connection.scalars(statement))
    except (SQLAlchemyError, OSError) as exc:
        raise HarborUnavailableError("the MCP login's table grants cannot be read") from exc
    return tuple(name for name in readable if name not in MCP_READER_TABLES)
