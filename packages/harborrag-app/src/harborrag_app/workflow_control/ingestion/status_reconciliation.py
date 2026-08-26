"""Temporal-to-task status reconciliation for ingestion API reads."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from harborrag_core.ingestion import IngestionTask, IngestionTaskState
from harborrag_runtime.errors import RuntimeConnectionError, WorkflowOperationError

from .ports import ClientProvider, PublicTaskStore
from .presenters import TERMINAL_STATES

_FAILED_EXECUTION_STATUSES = frozenset({"failed", "terminated", "timed_out"})
_STALE_QUEUED_RUNNING_TIMEOUT = timedelta(minutes=10)
_STALE_QUEUED_FAILURE_CODE = "worker_unavailable"
_STALE_QUEUED_FAILURE_STAGE = "workflow_dispatch"


class TaskStatusReconciler:
    def __init__(self, *, client_provider: ClientProvider) -> None:
        self._client_provider = client_provider

    async def reconcile(
        self,
        task: IngestionTask,
        store: PublicTaskStore,
    ) -> IngestionTask:
        if task.status in TERMINAL_STATES:
            return task
        try:
            client = await self._client_provider()
        except RuntimeConnectionError:
            return task
        execution_status = await self._execution_status(client, task.task_id)
        if execution_status is None:
            return task

        reconciled = await self._reconcile_temporal_terminal(task, store, execution_status)
        if reconciled is not None:
            return reconciled

        reconciled = await self._reconcile_stale_queued_running(
            task,
            store,
            client=client,
            execution_status=execution_status,
        )
        if reconciled is not None:
            return reconciled
        return task

    @staticmethod
    async def _execution_status(client: object, task_id: str) -> str | None:
        execution_status_reader = getattr(client, "execution_status", None)
        if not callable(execution_status_reader):
            return None
        try:
            return str(await execution_status_reader(task_id)).lower()
        except WorkflowOperationError:
            return None

    async def _reconcile_temporal_terminal(
        self,
        task: IngestionTask,
        store: PublicTaskStore,
        execution_status: str,
    ) -> IngestionTask | None:
        if execution_status in _FAILED_EXECUTION_STATUSES:
            return await self._transition_and_reload(
                store,
                task,
                IngestionTaskState.FAILED,
                summary={
                    **task.summary,
                    "failed_stage": "workflow_execution",
                    "error_code": f"execution_{execution_status}",
                },
            )

        if execution_status == "canceled":
            return await self._transition_and_reload(
                store,
                task,
                IngestionTaskState.CANCELLED,
                summary={
                    **task.summary,
                    "stage": "COMPLETED",
                },
            )
        return None

    async def _reconcile_stale_queued_running(
        self,
        task: IngestionTask,
        store: PublicTaskStore,
        *,
        client: object,
        execution_status: str,
    ) -> IngestionTask | None:
        if execution_status != "running" or not self._queued_running_is_stale(task):
            return None
        terminated = await self._terminate_if_supported(client, task.task_id)
        if not terminated:
            return None
        return await self._transition_and_reload(
            store,
            task,
            IngestionTaskState.FAILED,
            summary={
                **task.summary,
                "stage": "COMPLETED",
                "failed_stage": _STALE_QUEUED_FAILURE_STAGE,
                "error_code": _STALE_QUEUED_FAILURE_CODE,
            },
        )

    @staticmethod
    async def _terminate_if_supported(client: object, task_id: str) -> bool:
        terminator = getattr(client, "terminate", None)
        if not callable(terminator):
            return False
        try:
            await terminator(
                task_id,
                reason=(
                    "workflow remained queued without worker pickup "
                    f"for {int(_STALE_QUEUED_RUNNING_TIMEOUT.total_seconds())} seconds"
                ),
            )
            return True
        except WorkflowOperationError:
            return False

    @staticmethod
    async def _transition_and_reload(
        store: PublicTaskStore,
        task: IngestionTask,
        status: IngestionTaskState,
        *,
        summary: dict[str, object],
    ) -> IngestionTask:
        await store.transition(task.task_id, status, summary=summary)
        return (await store.get(task.task_id)) or task

    @staticmethod
    def _queued_running_is_stale(task: IngestionTask) -> bool:
        if task.status != IngestionTaskState.PENDING:
            return False
        if task.started_at is not None:
            return False
        if str(task.summary.get("stage", "")).upper() not in {"", "QUEUED"}:
            return False
        submitted_at = task.submitted_at
        if submitted_at is None:
            return False
        if submitted_at.tzinfo is None:
            submitted_at = submitted_at.replace(tzinfo=UTC)
        return datetime.now(UTC) - submitted_at >= _STALE_QUEUED_RUNNING_TIMEOUT
