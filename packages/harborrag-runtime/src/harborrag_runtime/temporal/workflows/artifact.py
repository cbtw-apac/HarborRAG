"""One independently retryable and resolvable artifact workflow."""

from __future__ import annotations

from dataclasses import replace

from temporalio import workflow
from temporalio.exceptions import ActivityError

from harborrag_runtime.temporal.identity import artifact_workflow_id
from harborrag_runtime.temporal.schemas import (
    ArtifactActivityInput,
    ArtifactActivityResult,
    ArtifactResult,
    ArtifactStage,
    ArtifactStageState,
    ArtifactStatus,
    ArtifactWorkflowInput,
    PendingResolution,
    ResolutionDecision,
    ResolutionReceipt,
)
from harborrag_runtime.temporal.task_queues import ActivityClass
from harborrag_runtime.temporal.workflows.execution import (
    execute_artifact_stage,
    execute_resolution,
)

_TERMINAL_STATUSES = frozenset(
    {
        ArtifactStatus.SUCCEEDED,
        ArtifactStatus.UNCHANGED,
        ArtifactStatus.FAILED,
        ArtifactStatus.SKIPPED,
        ArtifactStatus.QUARANTINED,
        ArtifactStatus.CANCELLED,
    }
)

_STAGE_ACTIVITY = {
    ArtifactStage.PREFLIGHT: ("harborrag.preflight_artifact", ActivityClass.DISCOVERY),
    ArtifactStage.FETCH: ("harborrag.fetch_artifact", ActivityClass.CONNECTOR),
    ArtifactStage.CHUNK: ("harborrag.chunk_artifact", ActivityClass.CHUNKING),
    ArtifactStage.INDEX: ("harborrag.index_artifact", ActivityClass.INDEXING),
    ArtifactStage.VALIDATE: ("harborrag.validate_artifact", ActivityClass.VALIDATION),
    ArtifactStage.FINALIZE: ("harborrag.finalize_artifact", ActivityClass.MAINTENANCE),
}


@workflow.defn(name="harborrag.artifact_ingestion")
class ArtifactIngestionWorkflow:
    def __init__(self) -> None:
        self._input: ArtifactWorkflowInput | None = None
        self._state: ArtifactStageState | None = None
        self._status = ArtifactStatus.PENDING
        self._pending: PendingResolution | None = None
        self._skip_requested = False
        self._cancel_requested = False

    @workflow.run
    async def run(self, request: ArtifactWorkflowInput) -> ArtifactResult:
        self._input = request
        self._state = ArtifactStageState(
            artifact=request.artifact,
            generation_id=request.generation_id,
            artifact_revision_id=request.artifact.artifact_revision_id,
        )
        self._status = ArtifactStatus.RUNNING

        try:
            while self._status not in _TERMINAL_STATUSES:
                if self._cancel_requested:
                    self._status = ArtifactStatus.CANCELLED
                    break
                if self._skip_requested:
                    self._status = ArtifactStatus.SKIPPED
                    break
                if self._pending is not None:
                    await self._notify_parent("artifact_waiting", self._pending)
                    await workflow.wait_condition(
                        lambda: (
                            self._pending is None or self._skip_requested or self._cancel_requested
                        )
                    )
                    continue
                result = await self._execute_current_stage()
                self._state = result.state
                self._status = result.status
                self._pending = result.pending_resolution
                if self._status is ArtifactStatus.WAITING_FOR_RESOLUTION:
                    if self._pending is None:
                        raise ValueError("resolution status requires a pending resolution")
                    self._pending = replace(
                        self._pending,
                        workflow_id=artifact_workflow_id(
                            request.run_id,
                            request.artifact.artifact_id,
                            request.attempt,
                        ),
                    )
        except ActivityError as exc:
            cause = exc.cause
            return ArtifactResult(
                artifact_id=request.artifact.artifact_id,
                status=ArtifactStatus.FAILED,
                artifact_revision_id=self._state.artifact_revision_id,
                generation_id=request.generation_id,
                error_type=type(cause).__name__ if cause is not None else type(exc).__name__,
                error_message=str(cause or exc)[:512],
            )

        return ArtifactResult(
            artifact_id=request.artifact.artifact_id,
            status=self._status,
            artifact_revision_id=self._state.artifact_revision_id,
            generation_id=request.generation_id,
            pending_resolution=self._pending,
        )

    async def _execute_current_stage(self) -> ArtifactActivityResult:
        assert self._input is not None and self._state is not None
        stage = self._state.stage
        if stage is ArtifactStage.PARSE:
            activity_class = (
                ActivityClass.OCR if self._input.artifact.requires_ocr else ActivityClass.PARSER
            )
            name = "harborrag.parse_artifact"
        else:
            name, activity_class = _STAGE_ACTIVITY[stage]
        return await execute_artifact_stage(
            name,
            self._activity_input(),
            activity_class,
            self._input.options,
        )

    def _activity_input(self) -> ArtifactActivityInput:
        assert self._input is not None and self._state is not None
        return ArtifactActivityInput(
            run_id=self._input.run_id,
            tenant_id=self._input.tenant_id,
            manifest_id=self._input.manifest_id,
            state=self._state,
        )

    async def _notify_parent(self, signal: str, value: object) -> None:
        parent = workflow.info().parent
        if parent is not None:
            handle = workflow.get_external_workflow_handle(parent.workflow_id)
            await handle.signal(signal, value)

    @workflow.query
    def get_status(self) -> ArtifactStatus:
        return self._status

    @workflow.query
    def get_pending_resolution(self) -> PendingResolution | None:
        return self._pending

    @workflow.signal
    def skip_artifact(self) -> None:
        self._skip_requested = True

    @workflow.signal
    def request_graceful_cancel(self) -> None:
        self._cancel_requested = True

    @workflow.update
    async def submit_resolution(self, decision: ResolutionDecision) -> ResolutionReceipt:
        assert self._input is not None and self._state is not None
        decision = replace(decision, submitted_at=workflow.now().isoformat())
        receipt = await execute_resolution(
            self._activity_input(),
            decision,
            self._input.options,
        )
        self._state = replace(self._state, stage=receipt.resume_stage)
        self._pending = None
        self._status = ArtifactStatus.RUNNING
        await self._notify_parent("artifact_resolution_completed", decision.artifact_id)
        return receipt

    @submit_resolution.validator
    def validate_resolution(self, decision: ResolutionDecision) -> None:
        if self._pending is None:
            raise ValueError("artifact is not waiting for a resolution")
        if decision.artifact_id != self._pending.artifact_id:
            raise ValueError("resolution artifact does not match the pending request")
        if decision.request_ref != self._pending.request_ref:
            raise ValueError("resolution request reference is stale")
