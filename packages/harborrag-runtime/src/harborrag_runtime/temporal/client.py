"""Client for starting, querying and controlling ingestion workflows in Temporal.

Errors raised to callers name the task and a safe error code, never a failure's text,
because API responses show them.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

from temporalio.api.common.v1 import WorkflowExecution
from temporalio.api.workflowservice.v1 import (
    DescribeWorkflowExecutionRequest,
    UnpauseWorkflowExecutionRequest,
)
from temporalio.client import (
    Client,
    WorkflowExecutionStatus,
    WorkflowFailureError,
    WorkflowHandle,
)
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from harborrag_core.ingestion.run_contracts import RunKind
from harborrag_core.observability.log_events import FailureKind, log_event, log_failure
from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.errors import (
    RuntimeConnectionError,
    WorkflowNotFoundError,
    WorkflowNotRunningError,
    WorkflowOperationError,
    WorkflowSubmissionError,
)

from .connection import connect_temporal_client
from .failure_handling import terminal_failure_code
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

logger = logging.getLogger("harborrag.runtime.temporal.client")

# TODO: drop once temporalio's WorkflowExecutionStatus knows PAUSED (absent through 1.35.0).
_NATIVE_EXECUTION_STATUSES = {8: "paused"}
"""Names of the execution status codes the SDK's enum does not know."""
_PAUSE_SETTLE_SECONDS = 15.0
"""How long a pause waits for the run to take it. The HTTP action then waits up to 10 s
for the paused state, so a pause request answers before a 30 s proxy timeout.
"""
_PAUSE_POLL_SECONDS = 0.1
"""How often a pause queries the run's status while it waits."""
_UNPAUSE_OPERATION = "unpause"


def _resumed(status: SourceIngestionStatus) -> bool:
    """Return whether the run holds no pause and no cancel, as after a resume."""
    return not status.paused and not status.cancel_requested


def _failure_message(label: str, code: str) -> str:
    """Return authored text plus the safe error code of a failed workflow run."""
    if code.startswith("discoversourceitems_") and code.endswith(
        ("_keyerror", "_connectorconfigurationerror")
    ):
        return (
            f"Could not {label}: the run failed (error_code={code}). The source's connection "
            "is missing or invalid; check its status with GET /v1/admin/connections and fix "
            "it with PUT /v1/admin/projects/{project_id}/connections/{connection_id}"
        )
    return (
        f"Could not {label}: the run failed (error_code={code}); "
        "check the task status or the worker logs"
    )


class IngestionTemporalClient:
    """Submit and inspect Postgres-authoritative source ingestion workflows.

    Args:
        client: Connected Temporal client.
        config: Task queues, timeouts and connection settings of the runtime.
    """

    def __init__(self, client: Client, config: TemporalRuntimeConfig) -> None:
        self._client = client
        self._config = config

    @classmethod
    async def connect(
        cls,
        config: TemporalRuntimeConfig,
    ) -> IngestionTemporalClient:
        """Connect to Temporal and return a client."""
        return cls(await connect_temporal_client(config), config)

    async def start(
        self,
        request: SourceIngestionInput,
    ) -> WorkflowHandle[SourceIngestionInput, SourceIngestionResult]:
        """Start a source ingestion workflow and return its handle."""
        request = replace(
            request,
            workflow_options=self._config.workflow_options().for_run(request.run_settings),
        )
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
        """Start a source ingestion workflow and return a reference to it."""
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
        """Start a retry workflow for failed documents and return a reference to it."""
        request = replace(request, workflow_options=self._config.workflow_options())
        workflow_id = RunKind.RETRY.workflow_id(request.retry_task_id)
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
        """Return whether Temporal answers a health check."""
        try:
            return bool(await self._client.service_client.check_health())
        except (RPCError, OSError, RuntimeError) as error:
            raise RuntimeConnectionError("Temporal workflow service health check failed") from error

    async def result(self, task_id: str) -> SourceIngestionResult:
        """Return the result of a finished source ingestion workflow."""
        return await self._operation(
            f"read result for ingestion task {task_id!r}",
            self._source_handle(task_id).result(),
        )

    async def progress(self, task_id: str) -> dict[str, int]:
        """Return document counts of a source ingestion workflow."""
        return await self.get_progress(task_id)

    async def get_progress(self, task_id: str) -> dict[str, int]:
        """Query the workflow for its document counts."""
        return await self._operation(
            f"read progress for ingestion task {task_id!r}",
            self._source_handle(task_id).query("get_progress", result_type=dict),
        )

    async def get_status(self, task_id: str) -> SourceIngestionStatus:
        """Query the workflow for its status."""
        return await self._operation(
            f"read status for ingestion task {task_id!r}",
            self._source_handle(task_id).query(
                "get_status",
                result_type=SourceIngestionStatus,
            ),
        )

    async def execution_status(self, task_id: str, kind: RunKind = RunKind.SOURCE) -> str:
        """Return the status of the task's workflow execution.

        Statuses newer than the SDK's enum, such as PAUSED, are read from the raw
        describe response, because the SDK raises on them.
        """
        workflow_id = kind.workflow_id(task_id)
        label = f"describe ingestion task {task_id!r}"
        try:
            description = await self._operation(
                label, self._client.get_workflow_handle(workflow_id).describe()
            )
        except ValueError as error:
            if "WorkflowExecutionStatus" not in str(error):
                raise
            return await self._raw_execution_status(label, workflow_id)
        return description.status.name.lower() if description.status is not None else "unknown"

    async def _raw_execution_status(self, label: str, workflow_id: str) -> str:
        """Read the execution status from the raw describe, naming statuses the SDK lacks."""
        raw = await self._operation(
            label,
            self._client.workflow_service.describe_workflow_execution(
                DescribeWorkflowExecutionRequest(
                    namespace=self._config.connection.namespace,
                    execution=WorkflowExecution(workflow_id=workflow_id),
                )
            ),
        )
        code = raw.workflow_execution_info.status
        try:
            return WorkflowExecutionStatus(code).name.lower()
        except ValueError:
            native = _NATIVE_EXECUTION_STATUSES.get(code)
            if native is None:
                raise WorkflowOperationError(
                    f"Could not {label}: unrecognised execution status {code}"
                ) from None
            return native

    async def pause(self, task_id: str) -> None:
        """Signal a source run to pause and wait for the run to take the pause.

        Returns once the relay applies the pause, or once a resume takes it back. After
        15 s it returns with the pause still pending at the next batch boundary, as during
        discovery or finalization. The run is not natively paused, because Temporal 1.31
        never dispatches a workflow task that was scheduled when a native pause landed, so
        the run would hang.

        Args:
            task_id: The source run to pause.

        Raises:
            WorkflowNotRunningError: The run's workflow already closed.
            WorkflowNotFoundError: The run has no workflow.
            WorkflowOperationError: Temporal could not be reached, or the run never showed
                the pause.
        """
        # TODO: pause natively again on Temporal server >= 1.32 with temporalio >= 1.35.
        await self._signal(task_id, "pause")
        await self._wait_for_pause_relay(task_id)

    async def resume(self, task_id: str) -> None:
        """Signal the workflow to resume and unpause its execution."""
        await self._signal(task_id, "resume")
        await self._unpause_execution(
            self._workflow_id(task_id),
            reason="Source ingestion resume requested",
            best_effort=True,
        )

    async def cancel(self, task_id: str, kind: RunKind = RunKind.SOURCE) -> None:
        """Cancel a run: a source run at its next safe boundary, a retry run through Temporal.

        A retry workflow has no graceful-cancel signal; Temporal cancellation makes it
        record the run as cancelled before it closes.

        Args:
            task_id: The run to cancel.
            kind: Whether the run is a source run or a retry run.

        Raises:
            WorkflowNotRunningError: The run's workflow already closed.
            WorkflowNotFoundError: The run has no workflow.
            WorkflowOperationError: Temporal could not be reached.
        """
        if kind is RunKind.RETRY:
            await self._control(
                task_id,
                f"cancel retry task {task_id!r}",
                self._client.get_workflow_handle(kind.workflow_id(task_id)).cancel(),
                kind,
            )
            return
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
        """Start a reindex workflow and return its handle."""
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
        """Return the result of a finished reindex workflow."""
        handle = self._client.get_workflow_handle(
            f"harborrag-reindex:{reindex_job_id}",
            result_type=ReindexResult,
        )
        result: ReindexResult = await self._operation(
            f"read result for reindex job {reindex_job_id!r}",
            handle.result(),
        )
        return result

    async def _signal(self, task_id: str, name: str) -> None:
        """Send a named signal to the task's source workflow."""
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
        kind: RunKind = RunKind.SOURCE,
    ) -> None:
        """Run a control operation, telling a closed run from a missing one."""
        try:
            await self._operation(label, operation)
        except WorkflowNotFoundError:
            raise await self._closed_or_missing(task_id, label, kind) from None

    async def _closed_or_missing(
        self,
        task_id: str,
        label: str,
        kind: RunKind = RunKind.SOURCE,
    ) -> WorkflowOperationError:
        """Return the error for a run that Temporal no longer finds running."""
        try:
            status = await self.execution_status(task_id, kind)
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
        """Wait for the run to apply a delivered pause, or accept one still pending.

        The workflow applies a pause at its next batch boundary, or through the
        relay while a batch runs. During discovery or finalization neither happens
        for minutes, but the signal is recorded and the run will honour it, so the
        request is accepted rather than reported as an outage. A resume that takes the
        pause back ends the wait as well. The wait is bounded by the clock, not by a
        number of queries, so slow queries cannot stretch it.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _PAUSE_SETTLE_SECONDS
        seen_pause = False
        while True:
            status = await self.get_status(task_id)
            if status.pause_applied:
                return
            if status.status in {"COMPLETED", "PARTIAL", "FAILED", "CANCELLED"}:
                raise WorkflowNotRunningError(
                    f"Could not pause ingestion task {task_id!r}: "
                    f"the task already finished ({status.status})"
                )
            if seen_pause and _resumed(status):
                log_event(logger, "temporal.pause_taken_back", task_id=task_id)
                return
            seen_pause = seen_pause or status.paused
            if loop.time() >= deadline:
                break
            await asyncio.sleep(_PAUSE_POLL_SECONDS)
        if status.paused and not status.cancel_requested:
            log_event(
                logger,
                "temporal.pause_pending",
                reason="applies_at_next_batch_boundary",
                task_id=task_id,
            )
            return
        raise WorkflowOperationError(
            f"Could not pause ingestion task {task_id!r}: pause relay did not settle"
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
        """Unpause the workflow natively, leaving one that is not paused as it is."""
        request = UnpauseWorkflowExecutionRequest(
            namespace=self._config.connection.namespace,
            workflow_id=workflow_id,
            identity=self._config.connection.identity or "harborrag-runtime",
            reason=reason,
            request_id=request_id or uuid4().hex,
        )
        await self._native_pause_control(
            _UNPAUSE_OPERATION,
            workflow_id,
            self._client.workflow_service.unpause_workflow_execution(request),
            allow_missing=allow_missing,
            best_effort=best_effort,
        )

    @staticmethod
    async def _native_pause_control(
        operation: str,
        workflow_id: str,
        call: Awaitable[object],
        *,
        allow_missing: bool,
        best_effort: bool = True,
    ) -> None:
        """Await a native pause control call, logging a best-effort failure instead of raising.

        Args:
            operation: Fixed name of the control, such as ``unpause``.
            workflow_id: The workflow the control targets.
            call: The pending workflow-service call.
            allow_missing: Treat a workflow Temporal does not find as done.
            best_effort: Log a failure instead of raising it.

        Raises:
            WorkflowNotFoundError: Not best effort, and the workflow does not exist.
            WorkflowOperationError: Not best effort, and Temporal rejected the call.
        """
        try:
            await call
        except RPCError as error:
            if error.status is RPCStatusCode.FAILED_PRECONDITION:
                return
            if allow_missing and error.status is RPCStatusCode.NOT_FOUND:
                return
            if not best_effort:
                label = f"{operation} workflow {workflow_id!r}"
                if error.status is RPCStatusCode.NOT_FOUND:
                    raise WorkflowNotFoundError(f"Could not {label}: task not found") from error
                raise WorkflowOperationError(f"Could not {label}") from error
            log_failure(
                logger,
                "temporal.workflow_control_failed",
                error,
                FailureKind.EXPECTED,
                operation=operation,
                workflow_id=workflow_id,
                rpc_status=error.status.name,
            )

    @staticmethod
    def _workflow_id(task_id: str) -> str:
        return RunKind.SOURCE.workflow_id(task_id)

    @staticmethod
    async def _operation[ResultT](
        label: str,
        operation: Awaitable[ResultT],
    ) -> ResultT:
        """Await a Temporal call, translating its failures into runtime errors."""
        try:
            return await operation
        except WorkflowFailureError as error:
            raise WorkflowOperationError(
                _failure_message(label, terminal_failure_code(error))
            ) from error
        except RPCError as error:
            if error.status is RPCStatusCode.NOT_FOUND:
                raise WorkflowNotFoundError(f"Could not {label}: task not found") from error
            raise WorkflowOperationError(f"Could not {label}") from error
