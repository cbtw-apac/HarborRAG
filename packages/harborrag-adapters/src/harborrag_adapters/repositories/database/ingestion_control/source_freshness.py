"""SQL implementation of the SourceFreshnessReader port over ``ingestion_tasks``.

The read crosses tenants. Hand it only to an operator surface. On Postgres the query runs
under a statement timeout, so a slow read fails instead of holding a connection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import case, func, select, text
from sqlalchemy.exc import SQLAlchemyError

from harborrag_core.contracts.errors import HarborUnavailableError
from harborrag_core.ingestion.states import IngestionTaskState
from harborrag_core.ports.source_freshness import (
    UNKNOWN_CONNECTOR_TYPE,
    SourceFreshness,
    SourceFreshnessReport,
)

from .schema import INGESTION_TASKS, SOURCE_SCOPES

if TYPE_CHECKING:
    from sqlalchemy import Select
    from sqlalchemy.engine import RowMapping
    from sqlalchemy.ext.asyncio import AsyncEngine

_STATEMENT_TIMEOUT = text("SET LOCAL statement_timeout = '5s'")
"""Bounds the read on Postgres; SQLite has no statement timeout."""
_SUCCEEDED_STATES = (IngestionTaskState.COMPLETED.value, IngestionTaskState.PARTIAL.value)


class SqlSourceFreshnessReader:
    """SourceFreshnessReader backed by each source's runs in ``ingestion_tasks``.

    A source is a ``(tenant_id, source_scope_id)`` pair with at least one run. Its
    connector type comes from its ``source_scopes`` record.

    Args:
        engine: Engine of the database that holds the ingestion tables.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def source_freshness(self, *, limit: int) -> SourceFreshnessReport:
        """Read each source's last success and latest run state, stalest first.

        Args:
            limit: The most sources to return.

        Returns:
            The sources read and how many exist in all.

        Raises:
            HarborUnavailableError: The database cannot be reached, read or answer in
                five seconds.
        """
        try:
            async with self._engine.begin() as connection:
                if connection.dialect.name == "postgresql":
                    await connection.execute(_STATEMENT_TIMEOUT)
                rows = (await connection.execute(_freshness_query(limit))).mappings().all()
        except (SQLAlchemyError, OSError) as exc:
            raise HarborUnavailableError("source freshness is unavailable") from exc
        return SourceFreshnessReport(
            sources=tuple(_freshness_from_row(row) for row in rows),
            source_count=int(rows[0]["source_count"]) if rows else 0,
        )


def _freshness_query(limit: int) -> Select[tuple[object, ...]]:
    """Select each source's latest run, last success and the number of sources.

    The latest run is the last submitted, with the task ID breaking ties. The source count
    is a window over every source, so it ignores the limit.
    """
    source = (INGESTION_TASKS.c.tenant_id, INGESTION_TASKS.c.source_scope_id)
    runs = select(
        INGESTION_TASKS.c.tenant_id,
        INGESTION_TASKS.c.source_scope_id,
        INGESTION_TASKS.c.status,
        func.row_number()
        .over(
            partition_by=source,
            order_by=(INGESTION_TASKS.c.submitted_at.desc(), INGESTION_TASKS.c.task_id.desc()),
        )
        .label("recency"),
        func.max(
            case((INGESTION_TASKS.c.status.in_(_SUCCEEDED_STATES), INGESTION_TASKS.c.completed_at))
        )
        .over(partition_by=source)
        .label("last_success_at"),
    ).subquery("source_runs")
    return (
        select(
            runs.c.tenant_id,
            runs.c.source_scope_id,
            runs.c.status,
            runs.c.last_success_at,
            func.coalesce(SOURCE_SCOPES.c.connector_type, UNKNOWN_CONNECTOR_TYPE).label(
                "connector_type"
            ),
            func.count().over().label("source_count"),
        )
        .select_from(
            runs.outerjoin(SOURCE_SCOPES, SOURCE_SCOPES.c.source_scope_id == runs.c.source_scope_id)
        )
        .where(runs.c.recency == 1)
        .order_by(
            runs.c.last_success_at.asc().nulls_first(),
            runs.c.tenant_id,
            runs.c.source_scope_id,
        )
        .limit(limit)
    )


def _freshness_from_row(row: RowMapping) -> SourceFreshness:
    """Map one selected row to the port's value."""
    return SourceFreshness(
        tenant_id=str(row["tenant_id"]),
        source_scope_id=str(row["source_scope_id"]),
        connector_type=str(row["connector_type"]),
        last_success_at=row["last_success_at"],
        last_run_status=IngestionTaskState(row["status"]),
    )
