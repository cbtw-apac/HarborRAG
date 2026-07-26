"""State and execution helpers for the top-level ingestion workflow."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from temporalio import workflow
from temporalio.workflow import ChildWorkflowHandle, ParentClosePolicy

from harborrag_runtime.temporal.identity import partition_workflow_id
from harborrag_runtime.temporal.schemas import (
    ArtifactOutcomeCheckpoint,
    ArtifactReference,
    ArtifactResult,
    ArtifactStatus,
    DiscoveryInput,
    DiscoveryResult,
    IngestionRunInput,
    PartitionResult,
    PendingResolution,
    RunContinuationState,
    RunProgress,
    RunStatus,
)
from harborrag_runtime.temporal.task_queues import ActivityClass

from .partition_inputs import build_partition_input
from .partition_results import partition_result
from .run_state import cancellation_received, record_pending


class IngestionWorkflowBase:
    """Own mutable run state and deterministic child-workflow helpers."""

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
        self._outcomes: dict[str, ArtifactOutcomeCheckpoint] = {}
        self._retry_attempt = 0

    def _restore_run_state(self, request: IngestionRunInput) -> None:
        """Rehydrate state carried explicitly across continue-as-new."""

        self._run_id = request.run_id
        self._status = RunStatus.PAUSED if request.paused else RunStatus.RUNNING
        self._paused = request.paused
        self._progress = request.progress
        continuation = request.continuation
        self._partition_concurrency = (
            continuation.partition_concurrency or request.options.partition_concurrency
        )
        self._artifact_concurrency = (
            continuation.artifact_concurrency or request.options.artifact_concurrency
        )
        self._pending = list(dict.fromkeys((*self._pending, *continuation.pending_resolutions)))
        self._retry_requested = list(
            dict.fromkeys((*self._retry_requested, *continuation.retry_requested))
        )
        self._cancel_requested = self._cancel_requested or continuation.cancel_requested
        self._outcomes = {
            item.reference.artifact_id: item for item in continuation.artifact_outcomes
        }
        self._retry_attempt = continuation.retry_attempt
        self._refresh_outcome_queries()

    async def _run_partitions(
        self,
        request: IngestionRunInput,
        partitions: tuple[tuple[ArtifactReference, ...], ...],
        references: dict[str, ArtifactReference],
        next_partition: int,
    ) -> tuple[int, int, int]:
        partition_offset = 0
        completed_partitions = 0
        completed_artifacts = 0
        while partition_offset < len(partitions):
            await workflow.wait_condition(lambda: not self._paused or self._cancel_requested)
            if cancellation_received(self._cancel_requested):
                break
            batch = partitions[partition_offset : partition_offset + self._partition_concurrency]
            partition_offset += len(batch)
            numbered = tuple((next_partition + index, items) for index, items in enumerate(batch))
            next_partition += len(batch)
            self._current_partition = numbered[0][0] if numbered else None
            self._active_partition_handles = [
                await workflow.start_child_workflow(
                    "harborrag.ingestion_partition",
                    build_partition_input(
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
                    partition_result(handle, number, items)
                    for handle, (number, items) in zip(
                        self._active_partition_handles,
                        numbered,
                        strict=True,
                    )
                )
            )
            self._active_partition_handles = []
            for result in results:
                self._aggregate(result, references)
                completed_partitions += 1
                completed_artifacts += result.progress.processed
        return next_partition, completed_partitions, completed_artifacts

    @staticmethod
    def _should_continue_as_new(
        request: IngestionRunInput,
        segment_partitions: int,
        segment_artifacts: int,
    ) -> bool:
        return (
            workflow.info().is_continue_as_new_suggested()
            or segment_partitions >= request.options.continue_after_partitions
            or segment_artifacts >= request.options.continue_after_artifacts
        )

    async def _discover(
        self,
        request: IngestionRunInput,
        cursor: str | None,
    ) -> DiscoveryResult:
        from . import ingestion

        page_size = request.options.partition_size * request.options.partition_concurrency
        if request.options.max_artifacts is not None:
            remaining = request.options.max_artifacts - self._progress.discovered
            page_size = min(page_size, remaining)
        return await ingestion.execute_activity(
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
        attempt: int,
    ) -> PartitionResult:
        handle = await workflow.start_child_workflow(
            "harborrag.ingestion_partition",
            build_partition_input(
                request,
                number,
                artifacts,
                self._artifact_concurrency,
                attempt=attempt,
            ),
            id=partition_workflow_id(request.run_id, number, attempt),
            task_queue=request.options.task_queues.chunking,
            result_type=PartitionResult,
            parent_close_policy=ParentClosePolicy.REQUEST_CANCEL,
        )
        self._active_partition_handles = [handle]
        result = await partition_result(handle, number, artifacts)
        self._active_partition_handles = []
        return result

    async def _run_requested_retry(
        self,
        request: IngestionRunInput,
        partition_number: int,
    ) -> PartitionResult | None:
        if not self._retry_requested:
            return None
        available = {
            artifact_id: checkpoint.reference for artifact_id, checkpoint in self._outcomes.items()
        }
        selected_ids = [
            artifact_id for artifact_id in self._retry_requested if artifact_id in available
        ]
        retry_refs = tuple(available[artifact_id] for artifact_id in selected_ids)
        selected = set(selected_ids)
        self._retry_requested = [
            artifact_id for artifact_id in self._retry_requested if artifact_id not in selected
        ]
        if not retry_refs:
            return None
        self._retry_attempt += 1
        return await self._retry_partition(
            request,
            partition_number,
            retry_refs,
            self._retry_attempt,
        )

    def _aggregate(
        self,
        result: PartitionResult,
        references: dict[str, ArtifactReference],
        *,
        replacing: bool = False,
    ) -> None:
        if replacing:
            self._progress = replace(
                self._progress,
                partitions=self._progress.partitions + 1,
            )
        else:
            progress = replace(
                result.progress,
                partitions=result.progress.partitions + 1,
            )
            self._progress = self._progress.merge(progress)

        artifact_results = result.artifact_results or self._legacy_artifact_results(result)
        for artifact_result in artifact_results:
            previous = self._outcomes.get(artifact_result.artifact_id)
            if replacing and previous is not None:
                self._progress = self._progress.replace_artifact(
                    previous.result.status,
                    artifact_result.status,
                )
            reference = references.get(artifact_result.artifact_id)
            if (
                artifact_result.status
                in {
                    ArtifactStatus.FAILED,
                    ArtifactStatus.QUARANTINED,
                }
                and reference is not None
            ):
                self._outcomes[artifact_result.artifact_id] = ArtifactOutcomeCheckpoint(
                    reference=reference,
                    result=artifact_result,
                )
            else:
                self._outcomes.pop(artifact_result.artifact_id, None)
                self._pending = [
                    item
                    for item in self._pending
                    if item.artifact_id != artifact_result.artifact_id
                ]
        self._refresh_outcome_queries()
        for pending in result.pending_resolutions:
            self._pending = record_pending(self._pending, pending)

    @staticmethod
    def _legacy_artifact_results(result: PartitionResult) -> tuple[ArtifactResult, ...]:
        return tuple(
            ArtifactResult(artifact_id=artifact_id, status=ArtifactStatus.FAILED)
            for artifact_id in result.failed_artifacts
        ) + tuple(
            ArtifactResult(
                artifact_id=artifact_id,
                status=ArtifactStatus.QUARANTINED,
            )
            for artifact_id in result.quarantined_artifacts
        )

    def _refresh_outcome_queries(self) -> None:
        self._failed = [
            artifact_id
            for artifact_id, checkpoint in self._outcomes.items()
            if checkpoint.result.status is ArtifactStatus.FAILED
        ]
        self._quarantined = [
            artifact_id
            for artifact_id, checkpoint in self._outcomes.items()
            if checkpoint.result.status is ArtifactStatus.QUARANTINED
        ]

    def _continuation_state(self) -> RunContinuationState:
        return RunContinuationState(
            artifact_outcomes=tuple(self._outcomes.values()),
            pending_resolutions=tuple(self._pending),
            retry_requested=tuple(self._retry_requested),
            partition_concurrency=self._partition_concurrency,
            artifact_concurrency=self._artifact_concurrency,
            retry_attempt=self._retry_attempt,
            cancel_requested=self._cancel_requested,
        )
