"""SQL implementations of the MCP reader-key ports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harborrag_core.ports.api_keys import ApiKeyRecord, AuthStoreUnavailable

from .schemas import ActivityRow, McpApiKeyRow

SessionFactory = async_sessionmaker[AsyncSession]


def _to_record(row: McpApiKeyRow) -> ApiKeyRecord:
    return ApiKeyRecord(
        key_id=row.key_id,
        tenant_id=row.tenant_id,
        owner=row.owner,
        name=row.name,
        secret_hash=row.secret_hash,
        environment=row.environment,
        created_at=row.created_at,
        created_by=row.created_by,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        revoked_by=row.revoked_by,
        revocation_reason=row.revocation_reason,
    )


def _audit(*, tenant_id: str, actor: str, verb: str, key_id: str, summary: str) -> ActivityRow:
    return ActivityRow(
        id=f"act_{uuid4().hex}",
        tenant_id=tenant_id,
        actor=actor,
        verb=verb,
        entity_type="mcp_api_key",
        entity_id=key_id,
        summary=summary,
        created_at=datetime.now(UTC),
    )


@dataclass(slots=True)
class SqlApiKeyReader:
    """ApiKeyReader over ``mcp_api_keys``: one primary-key lookup, never cached."""

    sessions: SessionFactory

    async def get_by_key_id(self, key_id: str) -> ApiKeyRecord | None:
        try:
            async with self.sessions() as session:
                row = await session.scalar(
                    sa.select(McpApiKeyRow).where(McpApiKeyRow.key_id == key_id)
                )
        except Exception as exc:  # noqa: BLE001 - every failure here means "cannot decide"
            # SQLAlchemyError covers query failures, but a refused connection or a
            # rejected password surfaces as OSError/asyncpg errors that SQLAlchemy
            # re-raises raw. Whatever it was, the answer is the same: deny, and let
            # the caller tell an outage from a bad key by the exception type.
            raise AuthStoreUnavailable("MCP key store is unavailable") from exc
        return _to_record(row) if row is not None else None


@dataclass(slots=True)
class SqlApiKeyManager:
    """ApiKeyManager over ``mcp_api_keys``; every write carries its audit row."""

    sessions: SessionFactory

    async def create(self, record: ApiKeyRecord) -> None:
        async with self.sessions.begin() as session:
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
                    revoked_at=record.revoked_at,
                    revoked_by=record.revoked_by,
                    revocation_reason=record.revocation_reason,
                )
            )
            session.add(
                _audit(
                    tenant_id=record.tenant_id,
                    actor=record.created_by,
                    verb="mcp_key.created",
                    key_id=record.key_id,
                    summary=f"MCP reader key {record.name!r} created for {record.owner}",
                )
            )

    async def revoke(self, key_id: str, *, at: datetime, by: str, reason: str) -> bool:
        async with self.sessions.begin() as session:
            row = await session.scalar(sa.select(McpApiKeyRow).where(McpApiKeyRow.key_id == key_id))
            if row is None or row.revoked_at is not None:
                return False
            self._revoke_row(session, row, at=at, by=by, reason=reason)
            return True

    async def revoke_by_owner(
        self, tenant_id: str, owner: str, *, at: datetime, by: str, reason: str
    ) -> list[str]:
        async with self.sessions.begin() as session:
            rows = (
                await session.scalars(
                    sa.select(McpApiKeyRow)
                    .where(
                        McpApiKeyRow.tenant_id == tenant_id,
                        McpApiKeyRow.owner == owner,
                        McpApiKeyRow.revoked_at.is_(None),
                    )
                    .order_by(McpApiKeyRow.created_at)
                )
            ).all()
            for row in rows:
                self._revoke_row(session, row, at=at, by=by, reason=reason)
            return [row.key_id for row in rows]

    async def list_for_tenant(self, tenant_id: str) -> list[ApiKeyRecord]:
        async with self.sessions() as session:
            rows = await session.scalars(
                sa.select(McpApiKeyRow)
                .where(McpApiKeyRow.tenant_id == tenant_id)
                .order_by(McpApiKeyRow.created_at.desc(), McpApiKeyRow.key_id)
            )
            return [_to_record(row) for row in rows]

    @staticmethod
    def _revoke_row(
        session: AsyncSession, row: McpApiKeyRow, *, at: datetime, by: str, reason: str
    ) -> None:
        row.revoked_at = at
        row.revoked_by = by
        row.revocation_reason = reason
        session.add(
            _audit(
                tenant_id=row.tenant_id,
                actor=by,
                verb="mcp_key.revoked",
                key_id=row.key_id,
                summary=f"MCP reader key {row.name!r} of {row.owner} revoked: {reason}",
            )
        )


__all__ = ["SqlApiKeyManager", "SqlApiKeyReader"]
