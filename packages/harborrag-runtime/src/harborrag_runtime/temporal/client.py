from __future__ import annotations

from collections.abc import Awaitable
from datetime import timedelta
from typing import TypeVar, cast

from temporalio.client import Client, TLSConfig, WorkflowHandle
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

from .identity import RuntimeWorkflowRef
from .maintenance_schemas import (
    ReindexInput,
    ReindexResult,
)
from .policies import DISCOVERY_QUEUE, INDEX_QUEUE
from .schemas import (
    RetryFailuresInput,
    RetryFailuresResult,
    SourceIngestionInput,
    SourceIngestionResult,
    SourceIngestionStatus,
)

ResultT = TypeVar("ResultT")


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
        connection = config.connection
        tls: bool | TLSConfig | None = None
        if connection.tls.enabled:
            tls = TLSConfig(
                server_root_ca_cert=connection.tls.server_root_ca_cert,
                domain=connection.tls.domain,
                client_cert=connection.tls.client_cert,
                client_private_key=connection.tls.client_private_key,
            )
        try:
            client = await Client.connect(
                connection.target,
                namespace=connection.namespace,
                identity=connection.identity,
                api_key=connection.api_key,
                tls=tls,
            )
        except (RPCError, OSError) as error:
            raise RuntimeConnectionError(
                f"Could not connect to Temporal target {connection.target!r}"
            ) from error
        return cls(client, config)

    async def start(
        self,
        request: SourceIngestionInput,
    ) -> WorkflowHandle[SourceIngestionInput, SourceIngestionResult]:
        try:
            return await self._client.start_workflow(
                "harborrag.source_ingestion",
                request,
                id=self._workflow_id(request.task_id),
                task_queue=DISCOVERY_QUEUE,
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
        workflow_id = f"harborrag-retry:{request.retry_task_id}"
        try:
            handle = await self._client.start_workflow(
                "harborrag.retry_failures",
                request,
                id=workflow_id,
                task_queue=DISCOVERY_QUEUE,
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
        except (RPCError, OSError) as error:
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

    async def resume(self, task_id: str) -> None:
        await self._signal(task_id, "resume")

    async def cancel(self, task_id: str) -> None:
        await self._control(
            task_id,
            f"request graceful cancellation for ingestion task {task_id!r}",
            self._source_handle(task_id).signal("request_graceful_cancel"),
        )

    async def start_reindex(
        self,
        request: ReindexInput,
    ) -> WorkflowHandle[ReindexInput, ReindexResult]:
        return await self._client.start_workflow(
            "harborrag.reindex",
            request,
            id=f"harborrag-reindex:{request.reindex_job_id}",
            task_queue=INDEX_QUEUE,
            execution_timeout=timedelta(seconds=self._config.workflow_execution_timeout_seconds),
            task_timeout=timedelta(seconds=self._config.workflow_task_timeout_seconds),
            id_reuse_policy=(WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY),
            result_type=ReindexResult,
        )

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
