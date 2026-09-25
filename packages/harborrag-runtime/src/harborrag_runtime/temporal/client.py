from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from dataclasses import replace
from datetime import timedelta
from typing import TypeVar, cast
from uuid import uuid4

from temporalio.api.workflowservice.v1 import (
    PauseWorkflowExecutionRequest,
    UnpauseWorkflowExecutionRequest,
)
from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.errors import (
    RuntimeConnectionError,
    WorkflowNotFoundError,
    WorkflowNotRunningError,
    WorkflowOperationError,
    WorkflowSubmissionError,
)

from .connection import connect_temporal_client
from .identity import RuntimeWorkflowRef
from .maintenance_schemas import (
    ReindexInput,
    ReindexResult,
)
from .schemas import (
    RetryFailuresInput,
    RetryFailuresResult,
    SourceIngestionInput,
    SourceIngestionResult,
    SourceIngestionStatus,
)

ResultT = TypeVar("ResultT")

logger = logging.getLogger("harborrag.runtime.temporal.client")


class IngestionTemporalClient:
    """Submit and inspect Postgres-authoritative source ingestion workflows."""

    def __init__(self, client: Client, config: TemporalRuntimeConfig) -> None:
        self._client = client
        self._config = config

    @classmethod
    async def connect(
        cls,
        config: TemporalRuntimeConfig,
    ) -> IngestionTemporalClient:
        return cls(await connect_temporal_client(config), config)

    async def start(
        self,
        request: SourceIngestionInput,
    ) -> WorkflowHandle[SourceIngestionInput, SourceIngestionResult]:
        request = replace(request, workflow_options=self._config.workflow_options())
        try:
            return await self._client.start_workflow(
                "harborrag.source_ingestion",
                request,
                id=self._workflow_id(request.task_id),
                task_queue=self._config.task_queues.discovery,
                execution_timeout=timedelta(
                    seconds=self._config.workflow_execution_timeout_seconds
                ),
                task_timeout=timedelta(seconds=self._config.workflow_task_timeout_seconds),
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                result_type=SourceIngestionResult,
            )
        except WorkflowAlreadyStartedError:
            return self._source_handle(request.task_id)
        except RPCError as error:
            raise WorkflowSubmissionError(
                f"Could not start ingestion task {request.task_id!r}"
            ) from error

    async def start_ingestion(
        self,
        request: SourceIngestionInput,
    ) -> RuntimeWorkflowRef:
        handle = await self.start(request)
        return RuntimeWorkflowRef(
            run_id=request.task_id,
            workflow_id=self._workflow_id(request.task_id),
            first_execution_run_id=handle.first_execution_run_id,
        )

    async def start_retry_failures(
        self,
        request: RetryFailuresInput,
    ) -> RuntimeWorkflowRef:
        request = replace(request, workflow_options=self._config.workflow_options())
        workflow_id = f"harborrag-retry:{request.retry_task_id}"
        try:
            handle = await self._client.start_workflow(
                "harborrag.retry_failures",
                request,
                id=workflow_id,
                task_queue=self._config.task_queues.discovery,
                execution_timeout=timedelta(
                    seconds=self._config.workflow_execution_timeout_seconds
                ),
                task_timeout=timedelta(seconds=self._config.workflow_task_timeout_seconds),
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                result_type=RetryFailuresResult,
            )
        except WorkflowAlreadyStartedError:
            handle = self._client.get_workflow_handle(workflow_id)
        except RPCError as error:
            raise WorkflowSubmissionError(
                f"Could not start retry task {request.retry_task_id!r}"
            ) from error
        return RuntimeWorkflowRef(
            run_id=request.retry_task_id,
            workflow_id=workflow_id,
            first_execution_run_id=handle.first_execution_run_id,
        )

    async def health(self) -> bool:
        try:
            return bool(await self._client.service_client.check_health())
        except (RPCError, OSError, RuntimeError) as error:
            raise RuntimeConnectionError("Temporal workflow service health check failed") from error

    async def result(self, task_id: str) -> SourceIngestionResult:
        return await self._operation(
            f"read result for ingestion task {task_id!r}",
            self._source_handle(task_id).result(),
        )

    async def progress(self, task_id: str) -> dict[str, int]:
        return await self.get_progress(task_id)

    async def get_progress(self, task_id: str) -> dict[str, int]:
        return await self._operation(
            f"read progress for ingestion task {task_id!r}",
            self._source_handle(task_id).query("get_progress", result_type=dict),
        )

    async def get_status(self, task_id: str) -> SourceIngestionStatus:
        return await self._operation(
            f"read status for ingestion task {task_id!r}",
            self._source_handle(task_id).query(
                "get_status",
                result_type=SourceIngestionStatus,
            ),
        )

    async def execution_status(self, task_id: str) -> str:
        description = await self._operation(
            f"describe ingestion task {task_id!r}",
            self._source_handle(task_id).describe(),
        )
        return description.status.name.lower() if description.status is not None else "unknown"

    async def pause(self, task_id: str) -> None:
        await self._signal(task_id, "pause")
        await self._wait_for_pause_relay(task_id)
        await self._pause_execution(
            self._workflow_id(task_id),
            reason="Source ingestion pause requested",
            best_effort=True,
        )

    async def resume(self, task_id: str) -> None:
        await self._signal(task_id, "resume")
        await self._unpause_execution(
            self._workflow_id(task_id),
            reason="Source ingestion resume requested",
            best_effort=True,
        )

    async def cancel(self, task_id: str) -> None:
        await self._control(
            task_id,
            f"request graceful cancellation for ingestion task {task_id!r}",
            self._source_handle(task_id).signal("request_graceful_cancel"),
        )
        await self._unpause_execution(
            self._workflow_id(task_id),
            reason="Source ingestion cancellation requested",
            best_effort=True,
        )

    async def start_reindex(
        self,
        request: ReindexInput,
    ) -> WorkflowHandle[ReindexInput, ReindexResult]:
        request = replace(request, workflow_options=self._config.workflow_options())
        workflow_id = f"harborrag-reindex:{request.reindex_job_id}"
        try:
            return await self._client.start_workflow(
                "harborrag.reindex",
                request,
                id=workflow_id,
                task_queue=self._config.task_queues.index,
                execution_timeout=timedelta(
                    seconds=self._config.workflow_execution_timeout_seconds
                ),
                task_timeout=timedelta(seconds=self._config.workflow_task_timeout_seconds),
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                result_type=ReindexResult,
            )
        except WorkflowAlreadyStartedError:
            return self._client.get_workflow_handle(workflow_id, result_type=ReindexResult)
        except RPCError as error:
            raise WorkflowSubmissionError(
                f"Could not start reindex job {request.reindex_job_id!r}"
            ) from error

    async def reindex_result(
        self,
        reindex_job_id: str,
    ) -> ReindexResult:
        handle = self._client.get_workflow_handle(
            f"harborrag-reindex:{reindex_job_id}",
            result_type=ReindexResult,
        )
        return cast(
            ReindexResult,
            await self._operation(
                f"read result for reindex job {reindex_job_id!r}",
                handle.result(),
            ),
        )

    async def _signal(self, task_id: str, name: str) -> None:
        await self._control(
            task_id,
            f"signal {name} for ingestion task {task_id!r}",
            self._source_handle(task_id).signal(name),
        )

    async def _control(
        self,
        task_id: str,
        label: str,
        operation: Awaitable[object],
    ) -> None:
        try:
            await self._operation(label, operation)
        except WorkflowNotFoundError:
            raise await self._closed_or_missing(task_id, label) from None

    async def _closed_or_missing(
        self,
        task_id: str,
        label: str,
    ) -> WorkflowOperationError:
        try:
            status = await self.execution_status(task_id)
        except WorkflowOperationError:
            return WorkflowNotFoundError(f"Could not {label}: task not found")
        return WorkflowNotRunningError(
            f"Could not {label}: the task already finished (execution {status})"
        )

    def _source_handle(
        self,
        task_id: str,
    ) -> WorkflowHandle[SourceIngestionInput, SourceIngestionResult]:
        return self._client.get_workflow_handle(
            self._workflow_id(task_id),
            result_type=SourceIngestionResult,
        )

    async def _wait_for_pause_relay(self, task_id: str) -> None:
        for _ in range(300):
            status = await self.get_status(task_id)
            if status.pause_applied:
                return
            if status.status in {"COMPLETED", "PARTIAL", "FAILED", "CANCELLED"}:
                raise WorkflowNotRunningError(
                    f"Could not pause ingestion task {task_id!r}: "
                    f"the task already finished ({status.status})"
                )
            await asyncio.sleep(0.1)
        raise WorkflowOperationError(
            f"Could not pause ingestion task {task_id!r}: pause relay did not settle"
        )

    async def _pause_execution(
        self,
        workflow_id: str,
        *,
        reason: str,
        request_id: str | None = None,
        allow_missing: bool = False,
        best_effort: bool = True,
    ) -> None:
        request = PauseWorkflowExecutionRequest(
            namespace=self._config.connection.namespace,
            workflow_id=workflow_id,
            identity=self._config.connection.identity or "harborrag-runtime",
            reason=reason,
            request_id=request_id or uuid4().hex,
        )
        await self._native_pause_control(
            f"pause workflow {workflow_id!r}",
            self._client.workflow_service.pause_workflow_execution(request),
            allow_missing=allow_missing,
            best_effort=best_effort,
        )

    async def _unpause_execution(
        self,
        workflow_id: str,
        *,
        reason: str,
        request_id: str | None = None,
        allow_missing: bool = False,
        best_effort: bool = True,
    ) -> None:
        request = UnpauseWorkflowExecutionRequest(
            namespace=self._config.connection.namespace,
            workflow_id=workflow_id,
            identity=self._config.connection.identity or "harborrag-runtime",
            reason=reason,
            request_id=request_id or uuid4().hex,
        )
        await self._native_pause_control(
            f"unpause workflow {workflow_id!r}",
            self._client.workflow_service.unpause_workflow_execution(request),
            allow_missing=allow_missing,
            best_effort=best_effort,
        )

    @staticmethod
    async def _native_pause_control(
        label: str,
        operation: Awaitable[object],
        *,
        allow_missing: bool,
        best_effort: bool = True,
    ) -> None:
        try:
            await operation
        except RPCError as error:
            if error.status is RPCStatusCode.FAILED_PRECONDITION:
                return
            if allow_missing and error.status is RPCStatusCode.NOT_FOUND:
                return
            if not best_effort:
                if error.status is RPCStatusCode.NOT_FOUND:
                    raise WorkflowNotFoundError(f"Could not {label}: task not found") from error
                raise WorkflowOperationError(f"Could not {label}") from error
            logger.warning("Could not %s (status=%s): %s", label, error.status, error)

    @staticmethod
    def _workflow_id(task_id: str) -> str:
        return f"harborrag-source:{task_id}"

    @staticmethod
    async def _operation(
        label: str,
        operation: Awaitable[ResultT],
    ) -> ResultT:
        try:
            return await operation
        except RPCError as error:
            if error.status is RPCStatusCode.NOT_FOUND:
                raise WorkflowNotFoundError(f"Could not {label}: task not found") from error
            raise WorkflowOperationError(f"Could not {label}") from error
