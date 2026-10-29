"""Per-connection reads and purges over the ingestion control tables.

Handles:
- the documents, source scopes and runs that belong to one connection
- cheap stats for connection listings
- dropping a connection's Postgres history once its projections are gone

A connection is identified here by ``(tenant_id, connection_id)``, where
``connection_id`` is the connection's ``source_key``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import Result, delete, func, select
from sqlalchemy.engine import CursorResult

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_core.ingestion import IngestionTask, IngestionTaskState

from .schema import DOCUMENTS, INGESTION_TASKS, SOURCE_SCOPES
from .tasks import _task_from_row

_ACTIVE_TASK_STATES = (
    IngestionTaskState.PENDING.value,
    IngestionTaskState.RUNNING.value,
    IngestionTaskState.PAUSED.value,
)


@dataclass(frozen=True, slots=True)
class ConnectionPurgeCounts:
    """Rows removed by a purge."""

    documents: int
    source_scopes: int
    tasks: int


class ConnectionRecordRepository:
    """Documents, scopes and runs of one connection."""

    def __init__(self, client: SQLAlchemyDBClient) -> None:
        self._client = client

    async def active_document_ids(self, *, tenant_id: str, connection_id: str) -> list[str]:
        """Documents of the connection that still have a published version."""
        statement = (
            select(DOCUMENTS.c.document_id)
            .where(
                DOCUMENTS.c.tenant_id == tenant_id,
                DOCUMENTS.c.connection_id == connection_id,
                DOCUMENTS.c.active_document_version_id.is_not(None),
            )
            .order_by(DOCUMENTS.c.document_id)
        )
        async with self._client.sessions() as session:
            return [str(value) for value in (await session.execute(statement)).scalars()]

    async def document_count(self, *, tenant_id: str, connection_id: str) -> int:
        """How many of the connection's documents are published."""
        statement = select(func.count()).where(
            DOCUMENTS.c.tenant_id == tenant_id,
            DOCUMENTS.c.connection_id == connection_id,
            DOCUMENTS.c.active_document_version_id.is_not(None),
        )
        async with self._client.sessions() as session:
            return int((await session.execute(statement)).scalar_one())

    async def source_scope_ids(self, *, tenant_id: str, connection_id: str) -> list[str]:
        """Every source scope the connection has been ingested under."""
        statement = (
            select(SOURCE_SCOPES.c.source_scope_id)
            .where(
                SOURCE_SCOPES.c.tenant_id == tenant_id,
                SOURCE_SCOPES.c.connection_id == connection_id,
            )
            .order_by(SOURCE_SCOPES.c.source_scope_id)
        )
        async with self._client.sessions() as session:
            return [str(value) for value in (await session.execute(statement)).scalars()]

    async def latest_task(self, *, tenant_id: str, connection_id: str) -> IngestionTask | None:
        """The connection's most recently submitted run, or None."""
        return await self._one_task(tenant_id, connection_id, active_only=False)

    async def active_task(self, *, tenant_id: str, connection_id: str) -> IngestionTask | None:
        """The connection's pending, running or paused run, or None."""
        return await self._one_task(tenant_id, connection_id, active_only=True)

    async def purge(self, *, tenant_id: str, connection_id: str) -> ConnectionPurgeCounts:
        """Delete the connection's runs, documents and scopes; child rows cascade.

        Call only after the documents' projections were cleaned up: cleanup
        jobs are among the rows that cascade away.
        """
        scopes = select(SOURCE_SCOPES.c.source_scope_id).where(
            SOURCE_SCOPES.c.tenant_id == tenant_id,
            SOURCE_SCOPES.c.connection_id == connection_id,
        )
        async with self._client.sessions.begin() as session:
            tasks = await session.execute(
                delete(INGESTION_TASKS).where(
                    INGESTION_TASKS.c.tenant_id == tenant_id,
                    INGESTION_TASKS.c.source_scope_id.in_(scopes.scalar_subquery()),
                )
            )
            documents = await session.execute(
                delete(DOCUMENTS).where(
                    DOCUMENTS.c.tenant_id == tenant_id,
                    DOCUMENTS.c.connection_id == connection_id,
                )
            )
            scope_rows = await session.execute(
                delete(SOURCE_SCOPES).where(
                    SOURCE_SCOPES.c.tenant_id == tenant_id,
                    SOURCE_SCOPES.c.connection_id == connection_id,
                )
            )
        return ConnectionPurgeCounts(
            documents=_rowcount(documents),
            source_scopes=_rowcount(scope_rows),
            tasks=_rowcount(tasks),
        )

    async def _one_task(
        self, tenant_id: str, connection_id: str, *, active_only: bool
    ) -> IngestionTask | None:
        """Return the newest run of the connection, optionally only an active one."""
        scopes = select(SOURCE_SCOPES.c.source_scope_id).where(
            SOURCE_SCOPES.c.tenant_id == tenant_id,
            SOURCE_SCOPES.c.connection_id == connection_id,
        )
        statement = select(INGESTION_TASKS).where(
            INGESTION_TASKS.c.tenant_id == tenant_id,
            INGESTION_TASKS.c.source_scope_id.in_(scopes.scalar_subquery()),
        )
        if active_only:
            statement = statement.where(INGESTION_TASKS.c.status.in_(_ACTIVE_TASK_STATES))
        statement = statement.order_by(
            INGESTION_TASKS.c.submitted_at.desc(), INGESTION_TASKS.c.task_id.desc()
        ).limit(1)
        async with self._client.sessions() as session:
            row = (await session.execute(statement)).mappings().one_or_none()
        return _task_from_row(row) if row is not None else None


def _rowcount(result: Result[Any]) -> int:
    """Rows a DML statement touched."""
    return int(cast(CursorResult[Any], result).rowcount or 0)
