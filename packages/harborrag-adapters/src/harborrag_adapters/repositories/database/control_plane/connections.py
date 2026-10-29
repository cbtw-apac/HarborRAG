"""SQLAlchemy ``ConnectionRepositoryPort`` over the ``sources`` table.

Handles:
- lookups by public identity, ``source_key`` and internal id
- the attention-first keyset listing shared with the in-memory fake
- compare-and-swap writes that advance ``version`` exactly once per overwrite
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from harborrag_core.contracts.errors import HarborConflictError, HarborPreconditionFailedError
from harborrag_core.domain.connection import (
    ATTENTION_STATUSES,
    Connection,
    ConnectionPutOutcome,
    ConnectionStatus,
)
from harborrag_core.domain.connection_paging import (
    connection_sort_key,
    decode_connection_cursor,
    encode_connection_cursor,
)

from .mapping import connection_to_domain
from .schemas import SourceRow
from .session import SessionFactory

_ATTENTION_RANK = sa.case((SourceRow.status.in_(sorted(ATTENTION_STATUSES)), 0), else_=1)


@dataclass(slots=True)
class SqlConnectionRepository:
    """ConnectionRepositoryPort over the sources table."""

    sessions: SessionFactory

    async def get(
        self, *, tenant_id: str, project_id: str, connection_id: str
    ) -> Connection | None:
        """One connection by its public identity, or None."""
        return await self._one(
            SourceRow.tenant_id == tenant_id,
            SourceRow.project_id == project_id,
            SourceRow.connection_id == connection_id,
        )

    async def get_by_source_key(self, *, tenant_id: str, source_key: str) -> Connection | None:
        """One connection by ``source_key``, or None."""
        return await self._one(
            SourceRow.tenant_id == tenant_id,
            SourceRow.source_key == source_key,
        )

    async def get_by_id(
        self, connection_pk: str, *, tenant_ids: frozenset[str] | None
    ) -> Connection | None:
        """One connection by internal id within ``tenant_ids``, or None."""
        conditions = [SourceRow.id == connection_pk]
        if tenant_ids is not None:
            conditions.append(SourceRow.tenant_id.in_(tenant_ids))
        return await self._one(*conditions)

    async def list(  # noqa: PLR0913 - mirrors the port's filters
        self,
        *,
        tenant_ids: frozenset[str] | None,
        project_id: str | None = None,
        source_type: str | None = None,
        status: ConnectionStatus | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> tuple[list[Connection], str | None]:
        """Connections needing attention first, then by project and ``connection_id``."""
        statement = sa.select(SourceRow).order_by(
            _ATTENTION_RANK, SourceRow.project_id, SourceRow.connection_id
        )
        if tenant_ids is not None:
            statement = statement.where(SourceRow.tenant_id.in_(tenant_ids))
        if project_id is not None:
            statement = statement.where(SourceRow.project_id == project_id)
        if source_type is not None:
            statement = statement.where(SourceRow.source_type == source_type)
        if status is not None:
            statement = statement.where(SourceRow.status == status)
        if cursor is not None:
            statement = statement.where(_after(*decode_connection_cursor(cursor)))
        if limit is not None:
            statement = statement.limit(limit + 1)
        async with self.sessions() as session:
            rows = list(await session.scalars(statement))
        if limit is None or len(rows) <= limit:
            return [connection_to_domain(row) for row in rows], None
        page = [connection_to_domain(row) for row in rows[:limit]]
        return page, encode_connection_cursor(connection_sort_key(page[-1]))

    async def put(
        self,
        connection: Connection,
        *,
        expected_version: int | None = None,
        create_only: bool = False,
    ) -> tuple[Connection, ConnectionPutOutcome]:
        """Create or overwrite a connection under the port's precondition rules."""
        try:
            async with self.sessions.begin() as session:
                row = await session.scalar(
                    sa.select(SourceRow).where(
                        SourceRow.tenant_id == connection.tenant_id,
                        SourceRow.project_id == connection.project_id,
                        SourceRow.connection_id == connection.connection_id,
                    )
                )
                if row is None:
                    if expected_version is not None:
                        raise HarborPreconditionFailedError("connection does not exist")
                    row = _new_row(connection)
                    session.add(row)
                    await session.flush()
                    return connection_to_domain(row), "created"
                if create_only:
                    raise HarborPreconditionFailedError("connection already exists")
                if expected_version is not None and expected_version != row.version:
                    raise HarborPreconditionFailedError("connection version does not match")
                updated = await _swap(session, row, connection)
                return updated, "overwritten"
        except IntegrityError as error:
            raise _integrity_conflict(create_only) from error

    async def set_status(
        self,
        *,
        tenant_id: str,
        source_key: str,
        status: ConnectionStatus,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Record a status change without touching ``version``."""
        statement = (
            sa.update(SourceRow)
            .where(SourceRow.tenant_id == tenant_id, SourceRow.source_key == source_key)
            .values(status=status, status_detail=detail)
        )
        async with self.sessions.begin() as session:
            await session.execute(statement)

    async def delete(self, *, tenant_id: str, project_id: str, connection_id: str) -> None:
        """Remove the row; a missing row is a no-op."""
        statement = sa.delete(SourceRow).where(
            SourceRow.tenant_id == tenant_id,
            SourceRow.project_id == project_id,
            SourceRow.connection_id == connection_id,
        )
        async with self.sessions.begin() as session:
            await session.execute(statement)

    async def _one(self, *conditions: sa.ColumnElement[bool]) -> Connection | None:
        """Return the single row matching ``conditions`` as a domain object."""
        async with self.sessions() as session:
            row = await session.scalar(sa.select(SourceRow).where(*conditions))
            return connection_to_domain(row) if row is not None else None


def _after(rank: int, project_id: str, connection_id: str) -> sa.ColumnElement[bool]:
    """Keyset predicate for rows strictly after ``(rank, project_id, connection_id)``."""
    return sa.or_(
        _ATTENTION_RANK > rank,
        sa.and_(_ATTENTION_RANK == rank, SourceRow.project_id > project_id),
        sa.and_(
            _ATTENTION_RANK == rank,
            SourceRow.project_id == project_id,
            SourceRow.connection_id > connection_id,
        ),
    )


def _new_row(connection: Connection) -> SourceRow:
    """Map a new connection onto a fresh row at version 1."""
    return SourceRow(
        id=connection.id,
        tenant_id=connection.tenant_id,
        project_id=connection.project_id,
        connection_id=connection.connection_id,
        source_key=connection.source_key,
        source_type=connection.source_type,
        name=connection.name,
        credentials=dict(connection.credentials),
        config_json=dict(connection.config),
        tuning=dict(connection.tuning),
        secret_refs=list(connection.secret_refs),
        schedule=connection.schedule,
        status=connection.status,
        status_detail=connection.status_detail,
        version=1,
        created_by=connection.created_by,
        updated_by=connection.updated_by,
        created_at=connection.created_at,
        updated_at=connection.updated_at,
    )


async def _swap(session: AsyncSession, row: SourceRow, connection: Connection) -> Connection:
    """Overwrite mutable columns only if ``row.version`` is still current."""
    read_version = row.version
    result = cast(
        CursorResult[Any],
        await session.execute(
            sa.update(SourceRow)
            .where(SourceRow.id == row.id, SourceRow.version == read_version)
            .values(
                source_type=connection.source_type,
                name=connection.name,
                credentials=dict(connection.credentials),
                config_json=dict(connection.config),
                tuning=dict(connection.tuning),
                secret_refs=list(connection.secret_refs),
                schedule=connection.schedule,
                status=connection.status,
                status_detail=connection.status_detail,
                version=read_version + 1,
                updated_by=connection.updated_by,
                updated_at=connection.updated_at,
            )
            .execution_options(synchronize_session=False)
        ),
    )
    if result.rowcount != 1:
        raise HarborPreconditionFailedError("connection changed during the write")
    await session.refresh(row)
    return connection_to_domain(row)


def _integrity_conflict(create_only: bool) -> HarborConflictError:
    """Map a unique-constraint race to the error the caller can act on."""
    if create_only:
        return HarborPreconditionFailedError("connection already exists")
    return HarborConflictError("connection identity or source_key is already used in this tenant")
