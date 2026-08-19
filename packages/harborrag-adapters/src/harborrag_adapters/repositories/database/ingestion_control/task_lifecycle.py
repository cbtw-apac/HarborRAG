from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import select, update

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_core.base import utc_now
from harborrag_core.contracts import HarborConflictError, HarborNotFoundError
from harborrag_core.ingestion import IngestionTaskState, reject_runtime_fields

from .row_values import required_mapping
from .schema import INGESTION_TASKS

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


class TaskLifecycleMixin:
    """Advance an ingestion task's durable state machine."""

    _client: SQLAlchemyDBClient

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
