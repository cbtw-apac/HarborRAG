"""HarborRAG-owned public client for Temporal ingestion operations."""

from __future__ import annotations

from collections.abc import Awaitable
from datetime import timedelta
from typing import TypeVar

from temporalio.client import Client, TLSConfig, WorkflowHandle
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.errors import (
    RuntimeConnectionError,
    WorkflowOperationError,
    WorkflowSubmissionError,
)
from harborrag_runtime.temporal.identity import (
    RuntimeWorkflowRef,
    artifact_workflow_id,
    ingestion_workflow_id,
)
from harborrag_runtime.temporal.models import (
    ConcurrencyUpdate,
    IngestionRunInput,
    IngestionSummary,
    PendingResolution,
    ResolutionDecision,
    ResolutionReceipt,
    RunProgress,
    WorkflowStatusView,
)

ResultT = TypeVar("ResultT")


class TemporalRuntimeClient:
    """Expose ingestion operations without leaking Temporal handles to callers."""

    def __init__(self, client: Client, config: TemporalRuntimeConfig) -> None:
        self._client = client
        self._config = config

    @classmethod
    async def connect(cls, config: TemporalRuntimeConfig) -> TemporalRuntimeClient:
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
        except (RPCError, OSError) as exc:
            raise RuntimeConnectionError(
                f"Could not connect to Temporal target {connection.target!r}"
            ) from exc
        return cls(client, config)

    async def start_ingestion(self, request: IngestionRunInput) -> RuntimeWorkflowRef:
        workflow_id = ingestion_workflow_id(request.run_id)
        try:
            handle = await self._client.start_workflow(
                "harborrag.ingestion_run",
                request,
                id=workflow_id,
                task_queue=self._config.task_queues.discovery,
                execution_timeout=timedelta(
                    seconds=self._config.workflow_execution_timeout_seconds
                ),
                task_timeout=timedelta(seconds=self._config.workflow_task_timeout_seconds),
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except (RPCError, WorkflowAlreadyStartedError) as exc:
            raise WorkflowSubmissionError(
                f"Could not start ingestion run {request.run_id!r}"
            ) from exc
        return RuntimeWorkflowRef(
            run_id=request.run_id,
            workflow_id=workflow_id,
            first_execution_run_id=handle.first_execution_run_id,
        )

    async def health(self) -> bool:
        """Check that the configured Temporal workflow service is reachable."""

        try:
            return bool(await self._client.service_client.check_health())
        except (RPCError, OSError) as exc:
            raise RuntimeConnectionError("Temporal workflow service health check failed") from exc

    async def result(self, run_id: str) -> IngestionSummary:
        handle = self._run_handle(run_id)
        return await self._operation(
            f"read result for ingestion run {run_id!r}",
            handle.result(),
        )

    async def get_status(self, run_id: str) -> WorkflowStatusView:
        return await self._query(run_id, "get_status", WorkflowStatusView)

    async def get_progress(self, run_id: str) -> RunProgress:
        return await self._query(run_id, "get_progress", RunProgress)

    async def get_failed_artifacts(self, run_id: str) -> tuple[str, ...]:
        return await self._query(run_id, "get_failed_artifacts", tuple)

    async def get_quarantined_artifacts(self, run_id: str) -> tuple[str, ...]:
        return await self._query(run_id, "get_quarantined_artifacts", tuple)

    async def get_pending_resolutions(self, run_id: str) -> tuple[PendingResolution, ...]:
        return await self._query(run_id, "get_pending_resolutions", tuple)

    async def get_current_partition(self, run_id: str) -> int | None:
        return await self._query(run_id, "get_current_partition", int)

    async def pause(self, run_id: str) -> None:
        await self._signal_run(run_id, "pause")

    async def resume(self, run_id: str) -> None:
        await self._signal_run(run_id, "resume")

    async def retry_failed(self, run_id: str, artifact_ids: tuple[str, ...]) -> None:
        await self._signal_run(run_id, "retry_failed", artifact_ids)

    async def adjust_concurrency(
        self,
        run_id: str,
        update: ConcurrencyUpdate,
    ) -> ConcurrencyUpdate:
        handle = self._run_handle(run_id)
        operation = handle.execute_update(
            "adjust_concurrency",
            update,
            result_type=ConcurrencyUpdate,
        )
        return await self._operation(f"adjust concurrency for {run_id!r}", operation)

    async def skip_artifact(self, run_id: str, artifact_id: str, *, attempt: int = 0) -> None:
        handle = self._artifact_handle(run_id, artifact_id, attempt)
        await self._operation(
            f"skip artifact {artifact_id!r}",
            handle.signal("skip_artifact"),
        )

    async def submit_resolution(
        self,
        run_id: str,
        decision: ResolutionDecision,
        *,
        attempt: int = 0,
    ) -> ResolutionReceipt:
        handle = self._artifact_handle(run_id, decision.artifact_id, attempt)
        operation = handle.execute_update(
            "submit_resolution",
            decision,
            result_type=ResolutionReceipt,
        )
        return await self._operation(
            f"submit resolution for artifact {decision.artifact_id!r}",
            operation,
        )

    async def cancel(self, run_id: str, *, graceful: bool = True) -> None:
        handle = self._run_handle(run_id)
        if graceful:
            await self._operation(
                f"request graceful cancellation for {run_id!r}",
                handle.signal("request_graceful_cancel"),
            )
        else:
            await self._operation(f"cancel ingestion run {run_id!r}", handle.cancel())

    async def _query(self, run_id: str, name: str, result_type: type[ResultT]) -> ResultT:
        handle = self._run_handle(run_id)
        operation = handle.query(name, result_type=result_type)
        return await self._operation(f"query {name} for {run_id!r}", operation)

    async def _signal_run(self, run_id: str, name: str, arg: object | None = None) -> None:
        handle = self._run_handle(run_id)
        operation = handle.signal(name) if arg is None else handle.signal(name, arg)
        await self._operation(f"signal {name} for {run_id!r}", operation)

    def _run_handle(self, run_id: str) -> WorkflowHandle:
        return self._client.get_workflow_handle(
            ingestion_workflow_id(run_id),
            result_type=IngestionSummary,
        )

    def _artifact_handle(
        self,
        run_id: str,
        artifact_id: str,
        attempt: int,
    ) -> WorkflowHandle:
        return self._client.get_workflow_handle(artifact_workflow_id(run_id, artifact_id, attempt))

    @staticmethod
    async def _operation(label: str, operation: Awaitable[ResultT]) -> ResultT:
        try:
            return await operation
        except RPCError as exc:
            raise WorkflowOperationError(f"Could not {label}") from exc
