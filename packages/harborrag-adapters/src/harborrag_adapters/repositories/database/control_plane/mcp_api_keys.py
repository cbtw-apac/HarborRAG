"""MCP API-key store: the legacy repository plus the Reader/Manager port adapters.

Rows are never deleted -- revocation and expiry are columns, so the table is
its own audit trail. PostgresApiKeyReader is the read-only view handed to the
MCP server; PostgresApiKeyManager is CLI-only and writes the key change and its
activity row in one transaction. Every driver failure surfaces as
AuthStoreUnavailable so the MCP server can fail closed (503) instead of guessing.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from harborrag_core.contracts.errors import AuthStoreUnavailable
from harborrag_core.ports.api_keys import ApiKeyRecord, AuditEvent, Revocation
from harborrag_core.security.api_keys import Environment

from .mapping import utc_now
from .schemas import ActivityRow
from .schemas_mcp_auth import McpApiKeyRow
from .session import SessionFactory

logger = logging.getLogger("harborrag.adapters.control_plane.mcp_api_keys")
_ENTITY_TYPE = "mcp_api_key"


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


def _to_record(row: McpApiKeyRow) -> ApiKeyRecord:
    return ApiKeyRecord(
        key_id=row.key_id,
        tenant_id=row.tenant_id,
        owner=row.owner,
        name=row.name,
        secret_hash=row.secret_hash,
        environment=cast("Environment", row.environment),
        created_at=row.created_at,
        created_by=row.created_by,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        revoked_by=row.revoked_by,
        revocation_reason=row.revocation_reason,
    )


@dataclass(slots=True)
class PostgresApiKeyReader:
    """ApiKeyReader for the MCP server -- read-only."""

    sessions: SessionFactory

    async def get_by_key_id(self, key_id: str) -> ApiKeyRecord | None:
        statement = sa.select(McpApiKeyRow).where(McpApiKeyRow.key_id == key_id)
        async with _store_errors("get"), self.sessions() as session:
            row = (await session.scalars(statement)).one_or_none()
        return _to_record(row) if row is not None else None


def _activity_row(event: AuditEvent, at: datetime) -> ActivityRow:
    return ActivityRow(
        id=f"act_{uuid4().hex}",
        tenant_id=event.tenant_id,
        actor=event.actor,
        verb=event.action,
        entity_type=_ENTITY_TYPE,
        entity_id=event.entity_id,
        summary=event.detail or event.action,
        created_at=at,
    )


@dataclass(slots=True)
class PostgresApiKeyManager:
    """ApiKeyManager for the CLI; every method is one transaction with its audit row."""

    sessions: SessionFactory

    async def create(self, record: ApiKeyRecord, audit: AuditEvent) -> None:
        async with _store_errors("create"), self.sessions.begin() as session:
            session.add(
                McpApiKeyRow(
                    key_id=record.key_id,
                    tenant_id=record.tenant_id,
                    owner=record.owner,
                    name=record.name,
                    secret_hash=record.secret_hash,
                    environment=record.environment,
                    created_at=record.created_at,
                    created_by=record.created_by,
                    expires_at=record.expires_at,
                )
            )
            session.add(_activity_row(audit, record.created_at))

    async def revoke(
        self, key_id: str, *, tenant_id: str, revocation: Revocation, audit: AuditEvent
    ) -> bool:
        """False when the key is unknown, another tenant's, or already revoked."""
        statement = (
            sa.update(McpApiKeyRow)
            .where(
                McpApiKeyRow.key_id == key_id,
                McpApiKeyRow.tenant_id == tenant_id,
                McpApiKeyRow.revoked_at.is_(None),
            )
            .values(
                revoked_at=revocation.at,
                revoked_by=revocation.by,
                revocation_reason=revocation.reason,
            )
            .execution_options(synchronize_session=False)
        )
        async with _store_errors("revoke"), self.sessions.begin() as session:
            result = cast("sa.CursorResult[Any]", await session.execute(statement))
            changed = result.rowcount == 1
            if changed:
                session.add(_activity_row(audit, revocation.at))
        return changed

    async def revoke_by_owner(
        self, tenant_id: str, owner: str, *, revocation: Revocation
    ) -> list[str]:
        """Revoke every live key of ``owner``; one audit row per revoked key."""
        statement = (
            sa.update(McpApiKeyRow)
            .where(
                McpApiKeyRow.tenant_id == tenant_id,
                McpApiKeyRow.owner == owner,
                McpApiKeyRow.revoked_at.is_(None),
            )
            .values(
                revoked_at=revocation.at,
                revoked_by=revocation.by,
                revocation_reason=revocation.reason,
            )
            .returning(McpApiKeyRow.key_id)
            .execution_options(synchronize_session=False)
        )
        async with _store_errors("revoke_by_owner"), self.sessions.begin() as session:
            key_ids = list((await session.scalars(statement)).all())
            session.add_all(
                _activity_row(
                    AuditEvent(
                        actor=revocation.by,
                        tenant_id=tenant_id,
                        action="mcp_key.revoked",
                        entity_id=key_id,
                        detail=revocation.reason,
                    ),
                    revocation.at,
                )
                for key_id in key_ids
            )
        return key_ids

    async def list_for_tenant(self, tenant_id: str) -> list[ApiKeyRecord]:
        """All keys of ``tenant_id``, newest first, including revoked and expired ones."""
        statement = (
            sa.select(McpApiKeyRow)
            .where(McpApiKeyRow.tenant_id == tenant_id)
            .order_by(McpApiKeyRow.created_at.desc(), McpApiKeyRow.key_id.desc())
        )
        async with _store_errors("list_for_tenant"), self.sessions() as session:
            rows = (await session.scalars(statement)).all()
        return [_to_record(row) for row in rows]
