"""SQL implementations of the MCP reader-key ports."""

from __future__ import annotations

from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harborrag_core.ports.api_keys import ApiKeyRecord, AuthStoreUnavailable

from .schemas import ActivityRow, McpApiKeyRow

SessionFactory = async_sessionmaker[AsyncSession]


@contextmanager
def _store_errors() -> Generator[None]:
    """Turn any failure to reach or query the key store into ``AuthStoreUnavailable``.

    ``IntegrityError`` passes through: a rejected row is a bad write (a duplicate
    key id, a value outside the constraints), not an outage, and must not be
    retried as one.
    """

    try:
        yield
    except IntegrityError:
        raise
    except Exception as exc:  # noqa: BLE001 - every other failure here means "cannot decide"
        # SQLAlchemyError covers query failures, but a refused connection or a
        # rejected password surfaces as OSError/asyncpg errors that SQLAlchemy
        # re-raises raw. Whatever it was, the answer is the same: deny, and let
        # the caller tell an outage from a bad key by the exception type.
        raise AuthStoreUnavailable("MCP key store is unavailable") from exc


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
        with _store_errors():
            async with self.sessions() as session:
                row = await session.scalar(
                    sa.select(McpApiKeyRow).where(McpApiKeyRow.key_id == key_id)
                )
        return _to_record(row) if row is not None else None


@dataclass(slots=True)
class SqlApiKeyManager:
    """ApiKeyManager over ``mcp_api_keys``; every write carries its audit row.

    Store failures raise ``AuthStoreUnavailable`` like the reader; a row the
    database rejects still raises ``IntegrityError``.
    """

    sessions: SessionFactory

    async def create(self, record: ApiKeyRecord) -> None:
        with _store_errors():
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
        with _store_errors():
            async with self.sessions.begin() as session:
                revoked = (
                    await session.execute(
                        self._revoke_statement(at=at, by=by, reason=reason).where(
                            McpApiKeyRow.key_id == key_id
                        )
                    )
                ).all()
                self._audit_revocations(session, revoked, by=by, reason=reason)
        return bool(revoked)

    async def revoke_by_owner(
        self, tenant_id: str, owner: str, *, at: datetime, by: str, reason: str
    ) -> list[str]:
        with _store_errors():
            async with self.sessions.begin() as session:
                result = await session.execute(
                    self._revoke_statement(at=at, by=by, reason=reason).where(
                        McpApiKeyRow.tenant_id == tenant_id,
                        McpApiKeyRow.owner == owner,
                    )
                )
                # RETURNING has no guaranteed order; keep the oldest-first contract.
                revoked = sorted(result.all(), key=lambda row: row.created_at)
                self._audit_revocations(session, revoked, by=by, reason=reason)
        return [row.key_id for row in revoked]

    async def list_for_tenant(self, tenant_id: str) -> list[ApiKeyRecord]:
        with _store_errors():
            async with self.sessions() as session:
                rows = await session.scalars(
                    sa.select(McpApiKeyRow)
                    .where(McpApiKeyRow.tenant_id == tenant_id)
                    .order_by(McpApiKeyRow.created_at.desc(), McpApiKeyRow.key_id)
                )
                return [_to_record(row) for row in rows]

    @staticmethod
    def _revoke_statement(*, at: datetime, by: str, reason: str) -> sa.Update:
        # The IS NULL guard lets the database, not an earlier SELECT, decide which
        # call revokes a key: an already-revoked key matches no row, so the first
        # revocation's time, actor and reason are never overwritten, even when two
        # revocations race.
        return (
            sa.update(McpApiKeyRow)
            .where(McpApiKeyRow.revoked_at.is_(None))
            .values(revoked_at=at, revoked_by=by, revocation_reason=reason)
            .returning(
                McpApiKeyRow.key_id,
                McpApiKeyRow.tenant_id,
                McpApiKeyRow.owner,
                McpApiKeyRow.name,
                McpApiKeyRow.created_at,
            )
            .execution_options(synchronize_session=False)
        )

    @staticmethod
    def _audit_revocations(
        session: AsyncSession, revoked: Sequence[sa.Row[Any]], *, by: str, reason: str
    ) -> None:
        for row in revoked:
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
