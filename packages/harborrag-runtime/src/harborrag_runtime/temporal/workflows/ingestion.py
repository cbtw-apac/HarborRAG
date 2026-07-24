"""Top-level discovery, partitioning, control, and continuation workflow."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any, cast

from temporalio import workflow
from temporalio.exceptions import ChildWorkflowError
from temporalio.workflow import ChildWorkflowHandle, ParentClosePolicy

from harborrag_runtime.temporal.identity import partition_workflow_id
from harborrag_runtime.temporal.schemas import (
    ArtifactReference,
    ConcurrencyUpdate,
    DiscoveryInput,
    DiscoveryResult,
    IngestionRunInput,
    IngestionSummary,
    PartitionInput,
    PartitionResult,
    PendingResolution,
    ReconciliationInput,
    RunProgress,
    RunStatus,
    WorkflowStatusView,
)
from harborrag_runtime.temporal.task_queues import ActivityClass
from harborrag_runtime.temporal.workflows.execution import (
    execute_activity,
    execute_reconciliation,
)


@workflow.defn(name="harborrag.ingestion_run")
class IngestionRunWorkflow:
    def __init__(self) -> None:
        self._run_id = ""
        self._status = RunStatus.PENDING
        self._progress = RunProgress()
        self._current_partition: int | None = None
        self._paused = False
        self._cancel_requested = False
        self._partition_concurrency = 1
        self._artifact_concurrency = 1
        self._active_partition_handles: list[ChildWorkflowHandle[PartitionResult, Any]] = []
        self._failed: list[str] = []
        self._quarantined: list[str] = []
        self._pending: list[PendingResolution] = []
        self._retry_requested: list[str] = []

    @workflow.run
    async def run(self, request: IngestionRunInput) -> IngestionSummary:
        self._run_id = request.run_id
        self._status = RunStatus.PAUSED if request.paused else RunStatus.RUNNING
        self._paused = request.paused
        self._progress = request.progress
        self._partition_concurrency = request.options.partition_concurrency
        self._artifact_concurrency = request.options.artifact_concurrency
        segment_partitions = 0
        segment_artifacts = 0
        cursor = request.source_cursor
        next_partition = request.next_partition

        while True:
            await workflow.wait_condition(lambda: not self._paused or self._cancel_requested)
            if self._cancellation_received():
                return await self._finish(request, cancelled=True)

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

            partition_offset = 0
            while partition_offset < len(partitions):
                await workflow.wait_condition(lambda: not self._paused or self._cancel_requested)
                if self._cancellation_received():
                    return await self._finish(request, cancelled=True)
                batch = partitions[
                    partition_offset : partition_offset + self._partition_concurrency
                ]
                partition_offset += len(batch)
                numbered = tuple(
                    (next_partition + index, items) for index, items in enumerate(batch)
                )
                next_partition += len(batch)
                self._current_partition = numbered[0][0] if numbered else None
                self._active_partition_handles = [
                    await workflow.start_child_workflow(
                        "harborrag.ingestion_partition",
                        _build_partition_input(
                            request,
                            number,
                            items,
                            self._artifact_concurrency,
                        ),
                        id=partition_workflow_id(request.run_id, number, 0),
                        task_queue=request.options.task_queues.chunking,
                        result_type=PartitionResult,
                        parent_close_policy=ParentClosePolicy.REQUEST_CANCEL,
                    )
                    for number, items in numbered
                ]
                results = await asyncio.gather(
                    *(
                        self._partition_result(handle, number, items)
                        for handle, (number, items) in zip(
                            self._active_partition_handles,
                            numbered,
                            strict=True,
                        )
                    )
                )
                self._active_partition_handles = []
                for result in results:
                    self._aggregate(result)
                    segment_partitions += 1
                    segment_artifacts += result.progress.processed

            await workflow.sleep(0)
            if self._retry_requested:
                retry_refs = tuple(
                    references[artifact_id]
                    for artifact_id in self._retry_requested
                    if artifact_id in references
                )
                self._retry_requested = []
                if retry_refs:
                    retry_result = await self._retry_partition(
                        request,
                        next_partition,
                        retry_refs,
                    )
                    next_partition += 1
                    self._aggregate(retry_result)
                    segment_partitions += 1
                    segment_artifacts += retry_result.progress.processed

            cursor = discovery.next_cursor
            self._current_partition = None
            if discovery.done:
                return await self._finish(request, cancelled=False)
            if (
                workflow.info().is_continue_as_new_suggested()
                or segment_partitions >= request.options.continue_after_partitions
                or segment_artifacts >= request.options.continue_after_artifacts
            ):
                workflow.continue_as_new(
                    replace(
                        request,
                        source_cursor=cursor,
                        next_partition=next_partition,
                        progress=self._progress,
                        paused=self._paused,
                    )
                )

    async def _discover(
        self,
        request: IngestionRunInput,
        cursor: str | None,
    ) -> DiscoveryResult:
        page_size = request.options.partition_size * request.options.partition_concurrency
        return await execute_activity(
            "harborrag.discover_artifacts",
            DiscoveryInput(
                run_id=request.run_id,
                tenant_id=request.tenant_id,
                manifest_id=request.manifest_id,
                connector_name=request.connector_name,
                cursor=cursor,
                page_size=page_size,
            ),
            DiscoveryResult,
            ActivityClass.DISCOVERY,
            request.options,
        )

    async def _retry_partition(
        self,
        request: IngestionRunInput,
        number: int,
        artifacts: tuple[ArtifactReference, ...],
    ) -> PartitionResult:
        handle = await workflow.start_child_workflow(
            "harborrag.ingestion_partition",
            _build_partition_input(
                request,
                number,
                artifacts,
                self._artifact_concurrency,
                attempt=1,
            ),
            id=partition_workflow_id(request.run_id, number, 1),
            task_queue=request.options.task_queues.chunking,
            result_type=PartitionResult,
            parent_close_policy=ParentClosePolicy.REQUEST_CANCEL,
        )
        self._active_partition_handles = [handle]
        result = await self._partition_result(handle, number, artifacts)
        self._active_partition_handles = []
        return result

    @staticmethod
    async def _partition_result(
        handle: ChildWorkflowHandle[PartitionResult, Any],
        number: int,
        artifacts: tuple[ArtifactReference, ...],
    ) -> PartitionResult:
        try:
            return cast("PartitionResult", await handle)
        except ChildWorkflowError:
            return PartitionResult(
                partition_number=number,
                progress=RunProgress(processed=len(artifacts), failed=len(artifacts)),
                failed_artifacts=tuple(item.artifact_id for item in artifacts),
            )

    def _aggregate(self, result: PartitionResult) -> None:
        progress = replace(result.progress, partitions=result.progress.partitions + 1)
        self._progress = self._progress.merge(progress)
        self._failed = list(dict.fromkeys((*self._failed, *result.failed_artifacts)))
        self._quarantined = list(dict.fromkeys((*self._quarantined, *result.quarantined_artifacts)))
        for pending in result.pending_resolutions:
            self._record_pending(pending)

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

    def _record_pending(self, pending: PendingResolution) -> None:
        self._pending = [item for item in self._pending if item.artifact_id != pending.artifact_id]
        self._pending.append(pending)

    def _cancellation_received(self) -> bool:
        """Read signal-mutated state without invalid static narrowing across awaits."""

        return self._cancel_requested

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
        self._record_pending(pending)

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


def _build_partition_input(
    request: IngestionRunInput,
    number: int,
    artifacts: tuple[ArtifactReference, ...],
    artifact_concurrency: int,
    *,
    attempt: int = 0,
) -> PartitionInput:
    options = replace(request.options, artifact_concurrency=artifact_concurrency)
    return PartitionInput(
        run_id=request.run_id,
        tenant_id=request.tenant_id,
        manifest_id=request.manifest_id,
        partition_number=number,
        artifacts=artifacts,
        generation_id=request.generation_id,
        options=options,
        attempt=attempt,
    )
