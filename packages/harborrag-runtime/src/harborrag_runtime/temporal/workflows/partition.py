"""Bounded artifact fan-out for one ingestion partition."""

from __future__ import annotations

import asyncio
from typing import Any, cast

from temporalio import workflow
from temporalio.exceptions import ChildWorkflowError
from temporalio.workflow import ChildWorkflowHandle, ParentClosePolicy

from harborrag_runtime.temporal.identity import artifact_workflow_id
from harborrag_runtime.temporal.models import (
    ArtifactReference,
    ArtifactResult,
    ArtifactStatus,
    ArtifactWorkflowInput,
    ConcurrencyUpdate,
    PartitionInput,
    PartitionResult,
    PendingResolution,
    RunProgress,
)

_ROLLING_ARTIFACT_POOL_PATCH = "rolling-artifact-pool-v1"


@workflow.defn(name="harborrag.ingestion_partition")
class IngestionPartitionWorkflow:
    def __init__(self) -> None:
        self._paused = False
        self._cancel_requested = False
        self._artifact_concurrency = 1
        self._progress = RunProgress()
        self._pending: list[PendingResolution] = []

    @workflow.run
    async def run(self, request: PartitionInput) -> PartitionResult:
        self._artifact_concurrency = request.options.artifact_concurrency
        if workflow.patched(_ROLLING_ARTIFACT_POOL_PATCH):
            completed_results = await self._run_rolling(request)
        else:
            completed_results = await self._run_batched(request)

        return PartitionResult(
            partition_number=request.partition_number,
            progress=self._progress,
            failed_artifacts=tuple(
                result.artifact_id
                for result in completed_results
                if result.status is ArtifactStatus.FAILED
            ),
            quarantined_artifacts=tuple(
                result.artifact_id
                for result in completed_results
                if result.status is ArtifactStatus.QUARANTINED
            ),
            pending_resolutions=tuple(self._pending),
        )

    async def _run_rolling(self, request: PartitionInput) -> list[ArtifactResult]:
        results: list[ArtifactResult | None] = [None] * len(request.artifacts)
        active: dict[asyncio.Task[ArtifactResult], int] = {}
        next_index = 0

        while next_index < len(request.artifacts) or active:
            if not active:
                await workflow.wait_condition(
                    lambda: not self._paused or self._cancel_requested
                )

            if self._cancel_requested and not active:
                for index in range(next_index, len(request.artifacts)):
                    result = self._cancelled(request.artifacts[index])
                    results[index] = result
                    self._progress = self._progress.add_artifact(result.status)
                break

            # Refill slots as soon as individual artifacts finish. This avoids a
            # slow artifact holding an entire fixed-size batch at a barrier.
            while (
                not self._paused
                and not self._cancel_requested
                and next_index < len(request.artifacts)
                and len(active) < self._artifact_concurrency
            ):
                reference = request.artifacts[next_index]
                handle = await workflow.start_child_workflow(
                    "harborrag.artifact_ingestion",
                    ArtifactWorkflowInput(
                        run_id=request.run_id,
                        tenant_id=request.tenant_id,
                        manifest_id=request.manifest_id,
                        artifact=reference,
                        generation_id=request.generation_id,
                        options=request.options,
                        attempt=request.attempt,
                    ),
                    id=artifact_workflow_id(
                        request.run_id,
                        reference.artifact_id,
                        request.attempt,
                    ),
                    task_queue=request.options.task_queues.chunking,
                    result_type=ArtifactResult,
                    parent_close_policy=ParentClosePolicy.REQUEST_CANCEL,
                )
                active[
                    asyncio.create_task(self._artifact_result(handle, reference))
                ] = next_index
                next_index += 1

            if not active:
                continue

            done, _ = await workflow.wait(
                active,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in sorted(done, key=active.__getitem__):
                index = active.pop(task)
                result = task.result()
                results[index] = result
                self._progress = self._progress.add_artifact(result.status)

        completed_results: list[ArtifactResult] = []
        for completed in results:
            if completed is None:
                raise RuntimeError("artifact pool exited before producing every result")
            completed_results.append(completed)
        return completed_results

    async def _run_batched(self, request: PartitionInput) -> list[ArtifactResult]:
        """Preserve event history for executions started before the rolling-pool patch."""

        results: list[ArtifactResult] = []
        offset = 0
        while offset < len(request.artifacts):
            await workflow.wait_condition(lambda: not self._paused or self._cancel_requested)
            if self._cancel_requested:
                remaining = request.artifacts[offset:]
                cancelled = [self._cancelled(reference) for reference in remaining]
                results.extend(cancelled)
                for result in cancelled:
                    self._progress = self._progress.add_artifact(result.status)
                break
            batch = request.artifacts[offset : offset + self._artifact_concurrency]
            offset += len(batch)
            handles = [
                await workflow.start_child_workflow(
                    "harborrag.artifact_ingestion",
                    ArtifactWorkflowInput(
                        run_id=request.run_id,
                        tenant_id=request.tenant_id,
                        manifest_id=request.manifest_id,
                        artifact=reference,
                        generation_id=request.generation_id,
                        options=request.options,
                        attempt=request.attempt,
                    ),
                    id=artifact_workflow_id(
                        request.run_id,
                        reference.artifact_id,
                        request.attempt,
                    ),
                    task_queue=request.options.task_queues.chunking,
                    result_type=ArtifactResult,
                    parent_close_policy=ParentClosePolicy.REQUEST_CANCEL,
                )
                for reference in batch
            ]
            batch_results = await asyncio.gather(
                *(
                    self._artifact_result(handle, reference)
                    for handle, reference in zip(handles, batch, strict=True)
                )
            )
            results.extend(batch_results)
            for result in batch_results:
                self._progress = self._progress.add_artifact(result.status)
        return results

    @staticmethod
    async def _artifact_result(
        handle: ChildWorkflowHandle[ArtifactResult, Any],
        reference: ArtifactReference,
    ) -> ArtifactResult:
        try:
            return cast("ArtifactResult", await handle)
        except ChildWorkflowError as exc:
            cause = exc.cause
            return ArtifactResult(
                artifact_id=reference.artifact_id,
                status=ArtifactStatus.FAILED,
                artifact_revision_id=reference.artifact_revision_id,
                error_type=type(cause).__name__ if cause is not None else type(exc).__name__,
                error_message=str(cause or exc)[:512],
            )

    @staticmethod
    def _cancelled(reference: ArtifactReference) -> ArtifactResult:
        return ArtifactResult(
            artifact_id=reference.artifact_id,
            artifact_revision_id=reference.artifact_revision_id,
            status=ArtifactStatus.CANCELLED,
        )

    async def _notify_parent(self, signal: str, value: object) -> None:
        parent = workflow.info().parent
        if parent is not None:
            await workflow.get_external_workflow_handle(parent.workflow_id).signal(signal, value)

    @workflow.query
    def get_progress(self) -> RunProgress:
        return self._progress

    @workflow.signal
    def pause(self) -> None:
        self._paused = True

    @workflow.signal
    def resume(self) -> None:
        self._paused = False

    @workflow.signal
    def request_graceful_cancel(self) -> None:
        self._cancel_requested = True

    @workflow.signal
    def adjust_concurrency(self, update: ConcurrencyUpdate) -> None:
        if update.artifact_concurrency < 1:
            raise ValueError("artifact concurrency must be positive")
        self._artifact_concurrency = update.artifact_concurrency

    @workflow.signal
    async def artifact_waiting(self, pending: PendingResolution) -> None:
        self._pending = [item for item in self._pending if item.artifact_id != pending.artifact_id]
        self._pending.append(pending)
        await self._notify_parent("artifact_waiting", pending)

    @workflow.signal
    async def artifact_resolution_completed(self, artifact_id: str) -> None:
        self._pending = [item for item in self._pending if item.artifact_id != artifact_id]
        await self._notify_parent("artifact_resolution_completed", artifact_id)
