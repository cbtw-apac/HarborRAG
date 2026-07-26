"""Top-level discovery, partitioning, control, and continuation workflow."""

from __future__ import annotations

from dataclasses import replace

from temporalio import workflow
from temporalio.exceptions import TemporalError

from harborrag_runtime.temporal.schemas import (
    ConcurrencyUpdate,
    IngestionRunInput,
    IngestionSummary,
    PendingResolution,
    ReconciliationInput,
    RunProgress,
    RunStatus,
    WorkflowStatusView,
)
from harborrag_runtime.temporal.workflows.execution import (
    execute_activity as execute_activity,
)
from harborrag_runtime.temporal.workflows.execution import (
    execute_reconciliation,
)

from .ingestion_base import IngestionWorkflowBase
from .run_state import cancellation_received, record_pending

_DURABLE_CONTINUATION_PATCH = "durable-retry-continuation-v1"


def _patch_enabled(patch_id: str) -> bool:
    """Support direct deterministic unit tests outside a workflow event loop."""

    try:
        return workflow.patched(patch_id)
    except TemporalError:
        return True


@workflow.defn(name="harborrag.ingestion_run")
class IngestionRunWorkflow(IngestionWorkflowBase):
    @workflow.run
    async def run(self, request: IngestionRunInput) -> IngestionSummary:
        self._restore_run_state(request)
        durable_continuation = _patch_enabled(_DURABLE_CONTINUATION_PATCH)
        segment_partitions = 0
        segment_artifacts = 0
        cursor = request.source_cursor
        next_partition = request.next_partition

        while True:
            await workflow.wait_condition(lambda: not self._paused or self._cancel_requested)
            if cancellation_received(self._cancel_requested):
                return await self._finish(request, cancelled=True)
            if (
                request.options.max_artifacts is not None
                and self._progress.discovered >= request.options.max_artifacts
            ):
                return await self._finish(request, cancelled=False)

            discovery = await self._discover(request, cursor)
            if not discovery.artifacts and not discovery.done:
                raise ValueError("discovery returned an empty non-terminal page")
            self._progress = replace(
                self._progress,
                discovered=self._progress.discovered + len(discovery.artifacts),
            )
            references = {item.artifact_id: item for item in discovery.artifacts}
            partitions = tuple(
                discovery.artifacts[index : index + request.options.partition_size]
                for index in range(
                    0,
                    len(discovery.artifacts),
                    request.options.partition_size,
                )
            )

            (
                next_partition,
                completed_partitions,
                completed_artifacts,
            ) = await self._run_partitions(
                request,
                partitions,
                references,
                next_partition,
            )
            segment_partitions += completed_partitions
            segment_artifacts += completed_artifacts
            if cancellation_received(self._cancel_requested):
                return await self._finish(request, cancelled=True)

            await workflow.sleep(0)
            retry_result = await self._run_requested_retry(
                request,
                next_partition,
            )
            if retry_result is not None:
                next_partition += 1
                retry_references = {
                    item.reference.artifact_id: item.reference for item in self._outcomes.values()
                }
                self._aggregate(
                    retry_result,
                    retry_references,
                    replacing=True,
                )
                segment_partitions += 1
                segment_artifacts += retry_result.progress.processed

            cursor = discovery.next_cursor
            self._current_partition = None
            limit_reached = (
                request.options.max_artifacts is not None
                and self._progress.discovered >= request.options.max_artifacts
            )
            if discovery.done or limit_reached:
                return await self._finish(request, cancelled=False)
            if self._should_continue_as_new(
                request,
                segment_partitions,
                segment_artifacts,
            ):
                if cancellation_received(self._cancel_requested):
                    return await self._finish(request, cancelled=True)
                workflow.continue_as_new(
                    replace(
                        request,
                        source_cursor=cursor,
                        next_partition=next_partition,
                        progress=self._progress,
                        paused=self._paused,
                        continuation=(
                            self._continuation_state()
                            if durable_continuation
                            else request.continuation
                        ),
                    )
                )

    async def _finish(
        self,
        request: IngestionRunInput,
        *,
        cancelled: bool,
    ) -> IngestionSummary:
        self._status = RunStatus.CANCELLING if cancelled else RunStatus.RUNNING
        reconciliation = await execute_reconciliation(
            ReconciliationInput(
                run_id=request.run_id,
                tenant_id=request.tenant_id,
                manifest_id=request.manifest_id,
                generation_id=request.generation_id,
                progress=self._progress,
                cancelled=cancelled,
            ),
            request.options,
        )
        self._status = RunStatus.CANCELLED if cancelled else reconciliation.status
        return IngestionSummary(
            run_id=request.run_id,
            manifest_id=request.manifest_id,
            status=self._status,
            progress=self._progress,
            reconciliation_ref=reconciliation.reconciliation_ref,
        )

    @workflow.query
    def get_status(self) -> WorkflowStatusView:
        return WorkflowStatusView(
            run_id=self._run_id,
            status=self._status,
            progress=self._progress,
            current_partition=self._current_partition,
            paused=self._paused,
            cancel_requested=self._cancel_requested,
        )

    @workflow.query
    def get_progress(self) -> RunProgress:
        return self._progress

    @workflow.query
    def get_failed_artifacts(self) -> tuple[str, ...]:
        return tuple(self._failed)

    @workflow.query
    def get_quarantined_artifacts(self) -> tuple[str, ...]:
        return tuple(self._quarantined)

    @workflow.query
    def get_pending_resolutions(self) -> tuple[PendingResolution, ...]:
        return tuple(self._pending)

    @workflow.query
    def get_current_partition(self) -> int | None:
        return self._current_partition

    @workflow.signal
    async def pause(self) -> None:
        self._paused = True
        self._status = RunStatus.PAUSED
        for handle in self._active_partition_handles:
            await handle.signal("pause")

    @workflow.signal
    async def resume(self) -> None:
        self._paused = False
        self._status = RunStatus.RUNNING
        for handle in self._active_partition_handles:
            await handle.signal("resume")

    @workflow.signal
    def retry_failed(self, artifact_ids: tuple[str, ...]) -> None:
        self._retry_requested = list(dict.fromkeys((*self._retry_requested, *artifact_ids)))

    @workflow.signal
    async def request_graceful_cancel(self) -> None:
        self._cancel_requested = True
        self._status = RunStatus.CANCELLING
        for handle in self._active_partition_handles:
            await handle.signal("request_graceful_cancel")

    @workflow.signal
    def artifact_waiting(self, pending: PendingResolution) -> None:
        self._pending = record_pending(self._pending, pending)

    @workflow.signal
    def artifact_resolution_completed(self, artifact_id: str) -> None:
        self._pending = [item for item in self._pending if item.artifact_id != artifact_id]

    @workflow.update
    async def adjust_concurrency(self, update: ConcurrencyUpdate) -> ConcurrencyUpdate:
        self._partition_concurrency = update.partition_concurrency
        self._artifact_concurrency = update.artifact_concurrency
        for handle in self._active_partition_handles:
            await handle.signal("adjust_concurrency", update)
        return update

    @adjust_concurrency.validator
    def validate_concurrency(self, update: ConcurrencyUpdate) -> None:
        if update.partition_concurrency < 1 or update.artifact_concurrency < 1:
            raise ValueError("partition and artifact concurrency must be positive")
