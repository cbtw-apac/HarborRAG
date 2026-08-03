from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_core.base import utc_now
from harborrag_core.contracts import HarborConflictError, HarborNotFoundError
from harborrag_core.ingestion import (
    IngestionTask,
    IngestionTaskState,
    TaskRegistration,
    reject_runtime_fields,
)
from harborrag_core.invariants import HarborInvariantError

from .row_values import DatabaseRow, optional_datetime, required_mapping, required_text
from .schema import INGESTION_TASKS
from .task_results import (
    StoredTaskDocumentResult,
    TaskDocumentPage,
    TaskDocumentResultsMixin,
)

__all__ = [
    "IngestionTaskRepository",
    "StoredTaskDocumentResult",
    "TaskDocumentPage",
    "TaskRegistration",
]

_TASK_TRANSITIONS = {
    IngestionTaskState.PENDING: frozenset(
        {
            IngestionTaskState.RUNNING,
            IngestionTaskState.FAILED,
            IngestionTaskState.CANCELLED,
        }
    ),
    IngestionTaskState.RUNNING: frozenset(
        {
            IngestionTaskState.COMPLETED,
            IngestionTaskState.PARTIAL,
            IngestionTaskState.FAILED,
            IngestionTaskState.CANCELLED,
        }
    ),
    IngestionTaskState.COMPLETED: frozenset(),
    IngestionTaskState.PARTIAL: frozenset(),
    IngestionTaskState.FAILED: frozenset(),
    IngestionTaskState.CANCELLED: frozenset(),
}
_RETRYABLE_FINALIZATION_STAGES = frozenset({"relation_repair", "removal_reconciliation"})


class IngestionTaskRepository(TaskDocumentResultsMixin):
    """Persist API task lifecycle and bounded per-document outcomes."""

    def __init__(self, client: SQLAlchemyDBClient) -> None:
        self._client = client

    async def create(self, task: IngestionTask) -> None:
        await self.register(task)

    async def register(
        self,
        task: IngestionTask,
        *,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> TaskRegistration:
        """Create a task or return the task reserved by the same public request."""

        if task.status != IngestionTaskState.PENDING:
            raise ValueError("new ingestion tasks must begin in PENDING")
        reject_runtime_fields(task.request)
        tenant_id = str(task.request.get("tenant_id") or "DEFAULT")
        self._validate_idempotency(idempotency_key, request_hash)
        now = utc_now()
        try:
            async with self._client.sessions.begin() as session:
                if idempotency_key is not None:
                    existing_key = await session.execute(
                        select(INGESTION_TASKS)
                        .where(
                            INGESTION_TASKS.c.tenant_id == tenant_id,
                            INGESTION_TASKS.c.idempotency_key == idempotency_key,
                        )
                        .with_for_update()
                    )
                    key_row = existing_key.mappings().one_or_none()
                    if key_row is not None:
                        return self._match_idempotent_task(
                            key_row,
                            request_hash=request_hash,
                        )
                result = await session.execute(
                    select(INGESTION_TASKS).where(INGESTION_TASKS.c.task_id == task.task_id)
                )
                row = result.mappings().one_or_none()
                if row is None:
                    await session.execute(
                        insert(INGESTION_TASKS).values(
                            task_id=task.task_id,
                            tenant_id=tenant_id,
                            source_scope_id=task.source_scope_id,
                            status=task.status.value,
                            submitted_at=now,
                            request=task.request,
                            summary=task.summary,
                            idempotency_key=idempotency_key,
                            request_hash=request_hash,
                        )
                    )
                    return TaskRegistration(
                        task.model_copy(update={"submitted_at": now}),
                        created=True,
                    )
                if (
                    row["source_scope_id"],
                    row["request"],
                ) != (task.source_scope_id, task.request):
                    raise HarborConflictError("ingestion task identity collision")
                return TaskRegistration(_task_from_row(row), created=False)
        except IntegrityError as error:
            if idempotency_key is not None:
                async with self._client.sessions() as session:
                    result = await session.execute(
                        select(INGESTION_TASKS).where(
                            INGESTION_TASKS.c.tenant_id == tenant_id,
                            INGESTION_TASKS.c.idempotency_key == idempotency_key,
                        )
                    )
                    row = result.mappings().one_or_none()
                if row is not None:
                    return self._match_idempotent_task(
                        row,
                        request_hash=request_hash,
                    )
            raise HarborConflictError("ingestion task identity collision") from error

    @staticmethod
    def _match_idempotent_task(
        row: DatabaseRow,
        *,
        request_hash: str | None,
    ) -> TaskRegistration:
        if row["request_hash"] != request_hash:
            raise HarborConflictError(
                "idempotency key was already used for a different ingestion request"
            )
        return TaskRegistration(_task_from_row(row), created=False)

    @staticmethod
    def _validate_idempotency(
        idempotency_key: str | None,
        request_hash: str | None,
    ) -> None:
        if (idempotency_key is None) != (request_hash is None):
            raise ValueError("idempotency key and request hash must be supplied together")
        if idempotency_key is None:
            return
        if not idempotency_key.strip() or len(idempotency_key) > 255:
            raise ValueError("idempotency key must contain between 1 and 255 characters")
        if request_hash is None:
            raise HarborInvariantError("request_hash must not be None here")
        if len(request_hash) != 64 or any(
            character not in "0123456789abcdef" for character in request_hash
        ):
            raise ValueError("request hash must be lowercase SHA-256 hexadecimal")

    async def transition(
        self,
        task_id: str,
        target: IngestionTaskState,
        *,
        summary: Mapping[str, object] | None = None,
    ) -> None:
        safe_summary = dict(summary or {})
        reject_runtime_fields(safe_summary)
        now = utc_now()
        async with self._client.sessions.begin() as session:
            result = await session.execute(
                select(INGESTION_TASKS)
                .where(INGESTION_TASKS.c.task_id == task_id)
                .with_for_update()
            )
            row = result.mappings().one_or_none()
            if row is None:
                raise HarborNotFoundError(f"ingestion task does not exist: {task_id}")
            current = IngestionTaskState(row["status"])
            if current == target:
                return
            if target not in _TASK_TRANSITIONS[current]:
                raise HarborConflictError(
                    f"invalid ingestion task transition: {current.value} -> {target.value}"
                )
            values: dict[str, object] = {
                "status": target.value,
                "summary": safe_summary,
            }
            if target == IngestionTaskState.RUNNING:
                values["started_at"] = now
            if target in {
                IngestionTaskState.COMPLETED,
                IngestionTaskState.PARTIAL,
                IngestionTaskState.FAILED,
                IngestionTaskState.CANCELLED,
            }:
                values["completed_at"] = now
            await session.execute(
                update(INGESTION_TASKS).where(INGESTION_TASKS.c.task_id == task_id).values(**values)
            )

    async def update_summary(
        self,
        task_id: str,
        values: Mapping[str, object],
    ) -> None:
        """Merge safe progress fields without changing the task lifecycle state."""

        safe_values = dict(values)
        reject_runtime_fields(safe_values)
        async with self._client.sessions.begin() as session:
            result = await session.execute(
                select(INGESTION_TASKS)
                .where(INGESTION_TASKS.c.task_id == task_id)
                .with_for_update()
            )
            row = result.mappings().one_or_none()
            if row is None:
                raise HarborNotFoundError(f"ingestion task does not exist: {task_id}")
            summary = required_mapping(row, "summary")
            summary.update(safe_values)
            await session.execute(
                update(INGESTION_TASKS)
                .where(INGESTION_TASKS.c.task_id == task_id)
                .values(summary=summary)
            )

    async def finalize(
        self,
        task_id: str,
        target: IngestionTaskState,
        *,
        summary: Mapping[str, object],
    ) -> None:
        """Converge a retried finalization activity on its durable outcome."""

        if target not in {
            IngestionTaskState.COMPLETED,
            IngestionTaskState.PARTIAL,
            IngestionTaskState.FAILED,
        }:
            raise ValueError("task finalization requires a terminal outcome")
        safe_summary = dict(summary)
        reject_runtime_fields(safe_summary)
        now = utc_now()
        async with self._client.sessions.begin() as session:
            result = await session.execute(
                select(INGESTION_TASKS)
                .where(INGESTION_TASKS.c.task_id == task_id)
                .with_for_update()
            )
            row = result.mappings().one_or_none()
            if row is None:
                raise HarborNotFoundError(f"ingestion task does not exist: {task_id}")
            current = IngestionTaskState(row["status"])
            if current in {
                IngestionTaskState.COMPLETED,
                IngestionTaskState.PARTIAL,
            }:
                if target == current:
                    return
                raise HarborConflictError("a finalized ingestion task cannot change outcome")
            if current == IngestionTaskState.FAILED:
                previous_summary = required_mapping(row, "summary")
                failed_stage = previous_summary.get("failed_stage")
                if failed_stage not in _RETRYABLE_FINALIZATION_STAGES:
                    if target == current:
                        return
                    raise HarborConflictError("a terminal ingestion failure cannot be completed")
            elif current != IngestionTaskState.RUNNING:
                raise HarborConflictError(
                    f"invalid ingestion task finalization from {current.value}"
                )
            await session.execute(
                update(INGESTION_TASKS)
                .where(INGESTION_TASKS.c.task_id == task_id)
                .values(
                    status=target.value,
                    summary=safe_summary,
                    completed_at=now,
                )
            )

    async def get(self, task_id: str) -> IngestionTask | None:
        """Load one durable task without exposing workflow runtime fields."""

        if not task_id.strip():
            raise ValueError("task_id must be non-empty")
        async with self._client.sessions() as session:
            result = await session.execute(
                select(INGESTION_TASKS).where(INGESTION_TASKS.c.task_id == task_id)
            )
            row = result.mappings().one_or_none()
            return _task_from_row(row) if row is not None else None

    async def pending_submissions(self, *, limit: int = 100) -> tuple[IngestionTask, ...]:
        """Return durable PENDING tasks whose Temporal start is not confirmed."""

        if not 1 <= limit <= 1_000:
            raise ValueError("pending submission limit must be between 1 and 1000")
        submission_state = INGESTION_TASKS.c.summary["submission_state"].as_string()
        async with self._client.sessions() as session:
            result = await session.execute(
                select(INGESTION_TASKS)
                .where(
                    INGESTION_TASKS.c.status == IngestionTaskState.PENDING.value,
                    or_(
                        submission_state.is_(None),
                        submission_state != "submitted",
                    ),
                )
                .order_by(INGESTION_TASKS.c.submitted_at, INGESTION_TASKS.c.task_id)
                .limit(limit)
            )
            return tuple(_task_from_row(row) for row in result.mappings().all())


def _task_from_row(row: DatabaseRow) -> IngestionTask:
    return IngestionTask(
        task_id=required_text(row, "task_id"),
        source_scope_id=required_text(row, "source_scope_id"),
        status=IngestionTaskState(required_text(row, "status")),
        submitted_at=optional_datetime(row, "submitted_at"),
        started_at=optional_datetime(row, "started_at"),
        completed_at=optional_datetime(row, "completed_at"),
        request=required_mapping(row, "request"),
        summary=required_mapping(row, "summary"),
    )
