from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_core.base import utc_now
from harborrag_core.ingestion import (
    StoredTaskDocumentResult,
    TaskDocumentPage,
    TaskDocumentResult,
    reject_runtime_fields,
)
from harborrag_core.invariants import HarborInvariantError
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId

from .row_values import (
    DatabaseRow,
    optional_text,
    required_datetime,
    required_mapping,
    required_text,
)
from .schema import TASK_DOCUMENT_RESULTS


class TaskDocumentResultsMixin:
    """Persist and query bounded per-document task outcomes."""

    _client: SQLAlchemyDBClient

    async def progress(self, task_id: str) -> dict[str, int]:
        async with self._client.sessions() as session:
            result = await session.execute(
                select(TASK_DOCUMENT_RESULTS.c.status, func.count().label("result_count"))
                .where(TASK_DOCUMENT_RESULTS.c.task_id == task_id)
                .group_by(TASK_DOCUMENT_RESULTS.c.status)
            )
            return {
                str(row["status"]).lower(): int(row["result_count"])
                for row in result.mappings().all()
            }

    async def progress_for_tasks(
        self,
        task_ids: Sequence[str],
    ) -> dict[str, dict[str, int]]:
        """Status counts for several tasks at once, keyed by task ID.

        The public task list renders progress for a whole page, so it groups
        by task in one query rather than issuing one ``progress`` call per
        row. Tasks with no recorded document outcome yet are simply absent
        from the result; callers treat a missing key as empty counts.
        """

        unique = tuple(dict.fromkeys(task_ids))
        if not unique:
            return {}
        async with self._client.sessions() as session:
            result = await session.execute(
                select(
                    TASK_DOCUMENT_RESULTS.c.task_id,
                    TASK_DOCUMENT_RESULTS.c.status,
                    func.count().label("result_count"),
                )
                .where(TASK_DOCUMENT_RESULTS.c.task_id.in_(unique))
                .group_by(TASK_DOCUMENT_RESULTS.c.task_id, TASK_DOCUMENT_RESULTS.c.status)
            )
            counts: dict[str, dict[str, int]] = {}
            for row in result.mappings().all():
                task_counts = counts.setdefault(str(row["task_id"]), {})
                task_counts[str(row["status"]).lower()] = int(row["result_count"])
            return counts

    async def record_document_result(self, result: TaskDocumentResult) -> None:
        reject_runtime_fields(result.result)
        async with self._client.sessions.begin() as session:
            values = {
                "task_id": result.task_id,
                "document_id": str(result.document_id),
                "document_version_id": (
                    str(result.document_version_id)
                    if result.document_version_id is not None
                    else None
                ),
                "status": result.status,
                "result": result.result,
                "completed_at": utc_now(),
            }
            insert_factory = (
                postgresql_insert if self._client.backend == "postgresql" else sqlite_insert
            )
            statement = insert_factory(TASK_DOCUMENT_RESULTS).values(**values)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["task_id", "document_id"],
                    set_={
                        "document_version_id": statement.excluded.document_version_id,
                        "status": statement.excluded.status,
                        "result": statement.excluded.result,
                        "completed_at": statement.excluded.completed_at,
                    },
                )
            )

    async def document_results(self, task_id: str) -> tuple[TaskDocumentResult, ...]:
        if not task_id.strip():
            raise ValueError("task_id must be non-empty")
        async with self._client.sessions() as session:
            rows = await session.execute(
                select(TASK_DOCUMENT_RESULTS)
                .where(TASK_DOCUMENT_RESULTS.c.task_id == task_id)
                .order_by(TASK_DOCUMENT_RESULTS.c.document_id)
            )
            return tuple(_typed_document_result(row) for row in rows.mappings().all())

    async def document_result(
        self,
        task_id: str,
        document_id: str,
    ) -> TaskDocumentResult | None:
        if not task_id.strip() or not document_id.strip():
            raise ValueError("task and document IDs must be non-empty")
        async with self._client.sessions() as session:
            result = await session.execute(
                select(TASK_DOCUMENT_RESULTS).where(
                    TASK_DOCUMENT_RESULTS.c.task_id == task_id,
                    TASK_DOCUMENT_RESULTS.c.document_id == document_id,
                )
            )
            row = result.mappings().one_or_none()
            return _typed_document_result(row) if row is not None else None

    async def document_results_page(
        self,
        task_id: str,
        *,
        statuses: Sequence[str] | None = None,
        after_updated_at: datetime | None = None,
        after_document_id: str | None = None,
        limit: int = 50,
    ) -> TaskDocumentPage:
        """Read a stable keyset page while new document outcomes are arriving."""

        if not task_id.strip():
            raise ValueError("task_id must be non-empty")
        if not 1 <= limit <= 200:
            raise ValueError("document result limit must be between 1 and 200")
        if (after_updated_at is None) != (after_document_id is None):
            raise ValueError("document result cursor values must be supplied together")
        statement = select(TASK_DOCUMENT_RESULTS).where(TASK_DOCUMENT_RESULTS.c.task_id == task_id)
        if statuses is not None:
            if not statuses:
                return TaskDocumentPage((), has_more=False)
            statement = statement.where(
                func.lower(TASK_DOCUMENT_RESULTS.c.status).in_(
                    tuple(status.lower() for status in statuses)
                )
            )
        if after_updated_at is not None:
            if after_document_id is None:
                raise HarborInvariantError("after_document_id must not be None here")
            statement = statement.where(
                or_(
                    TASK_DOCUMENT_RESULTS.c.completed_at > after_updated_at,
                    and_(
                        TASK_DOCUMENT_RESULTS.c.completed_at == after_updated_at,
                        TASK_DOCUMENT_RESULTS.c.document_id > after_document_id,
                    ),
                )
            )
        async with self._client.sessions() as session:
            result = await session.execute(
                statement.order_by(
                    TASK_DOCUMENT_RESULTS.c.completed_at,
                    TASK_DOCUMENT_RESULTS.c.document_id,
                ).limit(limit + 1)
            )
            rows = result.mappings().all()
        return TaskDocumentPage(
            items=tuple(_stored_document_result(row) for row in rows[:limit]),
            has_more=len(rows) > limit,
        )


def _typed_document_result(row: DatabaseRow) -> TaskDocumentResult:
    version_id = optional_text(row, "document_version_id")
    return TaskDocumentResult(
        task_id=required_text(row, "task_id"),
        document_id=DocumentId(required_text(row, "document_id")),
        document_version_id=(DocumentVersionId(version_id) if version_id else None),
        status=required_text(row, "status"),
        result=required_mapping(row, "result"),
    )


def _stored_document_result(row: DatabaseRow) -> StoredTaskDocumentResult:
    return StoredTaskDocumentResult(
        task_id=required_text(row, "task_id"),
        document_id=required_text(row, "document_id"),
        document_version_id=optional_text(row, "document_version_id"),
        status=required_text(row, "status"),
        result=required_mapping(row, "result"),
        updated_at=required_datetime(row, "completed_at"),
    )
