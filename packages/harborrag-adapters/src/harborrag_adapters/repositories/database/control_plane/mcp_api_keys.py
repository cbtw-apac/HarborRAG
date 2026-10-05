"""SqlMcpApiKeyRepository: the shared MCP API-key store.

Rows are never deleted -- revocation and expiry are columns, so the table is
its own audit trail. Every driver failure surfaces as AuthStoreUnavailable so
the MCP server can fail closed (503) instead of guessing.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from harborrag_core.contracts.errors import AuthStoreUnavailable

from .mapping import utc_now
from .schemas_mcp_auth import McpApiKeyRow
from .session import SessionFactory

logger = logging.getLogger("harborrag.adapters.control_plane.mcp_api_keys")


@asynccontextmanager
async def _store_errors(operation: str) -> AsyncGenerator[None, None]:
    """Map driver/ORM failures to AuthStoreUnavailable without leaking SQL parameters."""
    try:
        yield
    except (SQLAlchemyError, OSError, TimeoutError) as exc:
        # str(exc) carries bound parameters (secret_hash); log the type only.
        logger.error("MCP API-key store failed op=%s error=%s", operation, type(exc).__name__)
        raise AuthStoreUnavailable(f"MCP API-key store unavailable during {operation}") from None


@dataclass(slots=True)
class SqlMcpApiKeyRepository:
    """MCP API-key rows; insert, look up and revoke -- never delete."""

    sessions: SessionFactory

    async def insert(self, row: McpApiKeyRow) -> None:
        async with _store_errors("insert"), self.sessions.begin() as session:
            session.add(row)

    async def get(self, key_id: str) -> McpApiKeyRow | None:
        statement = sa.select(McpApiKeyRow).where(McpApiKeyRow.key_id == key_id)
        async with _store_errors("get"), self.sessions() as session:
            return (await session.scalars(statement)).one_or_none()

    async def revoke(
        self,
        key_id: str,
        *,
        tenant_id: str,
        revoked_by: str,
        reason: str | None = None,
    ) -> bool:
        """Revoke a live key; False when it is unknown, another tenant's, or already revoked."""
        statement = (
            sa.update(McpApiKeyRow)
            .where(
                McpApiKeyRow.key_id == key_id,
                McpApiKeyRow.tenant_id == tenant_id,
                McpApiKeyRow.revoked_at.is_(None),
            )
            .values(revoked_at=utc_now(), revoked_by=revoked_by, revocation_reason=reason)
            .execution_options(synchronize_session=False)
        )
        async with _store_errors("revoke"), self.sessions.begin() as session:
            result = cast("sa.CursorResult[Any]", await session.execute(statement))
        return result.rowcount == 1
