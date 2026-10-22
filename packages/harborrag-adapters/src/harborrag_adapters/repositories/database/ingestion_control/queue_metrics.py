"""Point-in-time ingestion queue counts behind the worker's live gauges."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, func, select

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_core.ingestion import DocumentVersionState, IngestionTaskState

from .schema import DOCUMENT_VERSIONS, INGESTION_TASKS, SOURCE_SCOPES, TASK_DOCUMENT_RESULTS
from .summary_schema import SUMMARY_SCOPES

IN_PROGRESS_STATES = (
    DocumentVersionState.RAW_CAPTURED,
    DocumentVersionState.CANONICAL_READY,
    DocumentVersionState.CHUNKS_READY,
    DocumentVersionState.REPRESENTATIONS_READY,
    DocumentVersionState.PROJECTIONS_STAGED,
    DocumentVersionState.VERIFIED,
)
ENRICHMENT_PENDING_EXECUTIONS = ("queued", "blocked", "failed", "running")


@dataclass(frozen=True, slots=True)
class IngestionQueueCounts:
    pending_by_connector: dict[str, int]
    # Start of the oldest running task that still has documents without an outcome.
    oldest_pending_at: datetime | None
    in_progress_by_state: dict[str, int]
    enrichment_pending: int


class IngestionQueueMetricsRepository:
    def __init__(self, client: SQLAlchemyDBClient) -> None:
        self._client = client

    async def counts(self) -> IngestionQueueCounts:
        versions = DOCUMENT_VERSIONS.c
        tasks = INGESTION_TASKS.c
        running = tasks.status == IngestionTaskState.RUNNING.value
        # A document version row appears only once parsing finishes, so the backlog
        # comes from tasks instead: documents discovery admitted that have no
        # outcome yet, whether still queued in Temporal or partway through.
        finished = (
            select(TASK_DOCUMENT_RESULTS.c.task_id, func.count().label("finished"))
            .where(TASK_DOCUMENT_RESULTS.c.task_id.in_(select(tasks.task_id).where(running)))
            .group_by(TASK_DOCUMENT_RESULTS.c.task_id)
            .subquery()
        )
        remaining = func.coalesce(tasks.summary["admitted"].as_integer(), 0) - func.coalesce(
            finished.c.finished, 0
        )
        backlog = case((remaining > 0, remaining), else_=0)
        async with self._client.sessions() as session:
            pending = (
                await session.execute(
                    select(
                        SOURCE_SCOPES.c.connector_type,
                        func.sum(backlog),
                        func.min(
                            case(
                                (remaining > 0, func.coalesce(tasks.started_at, tasks.submitted_at))
                            )
                        ),
                    )
                    .select_from(
                        INGESTION_TASKS.outerjoin(
                            finished, finished.c.task_id == tasks.task_id
                        ).outerjoin(
                            SOURCE_SCOPES, SOURCE_SCOPES.c.source_scope_id == tasks.source_scope_id
                        )
                    )
                    .where(running)
                    # Group by the bare column: PostgreSQL treats two bound literals in
                    # SELECT and GROUP BY as different expressions and rejects the query.
                    .group_by(SOURCE_SCOPES.c.connector_type)
                )
            ).tuples()
            in_progress = (
                await session.execute(
                    select(versions.status, func.count())
                    .where(versions.status.in_([state.value for state in IN_PROGRESS_STATES]))
                    .group_by(versions.status)
                )
            ).tuples()
            enrichment_pending = (
                await session.execute(
                    select(func.count())
                    .select_from(SUMMARY_SCOPES)
                    .where(
                        SUMMARY_SCOPES.c.policy.is_not(None),
                        SUMMARY_SCOPES.c.execution.in_(ENRICHMENT_PENDING_EXECUTIONS),
                    )
                )
            ).scalar_one()
            pending_by_connector: dict[str, int] = {}
            oldest: datetime | None = None
            for connector_type, count, started_at in pending:
                # A scope the catalog has not registered yet has no connector type.
                label = "unknown" if connector_type is None else str(connector_type)
                pending_by_connector[label] = int(count)
                if started_at is not None and (oldest is None or started_at < oldest):
                    oldest = started_at
            return IngestionQueueCounts(
                pending_by_connector=pending_by_connector,
                oldest_pending_at=oldest,
                in_progress_by_state={str(status): int(count) for status, count in in_progress},
                enrichment_pending=int(enrichment_pending),
            )
