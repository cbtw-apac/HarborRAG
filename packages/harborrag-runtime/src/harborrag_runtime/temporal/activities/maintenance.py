"""Finalization, reconciliation, cleanup, and manual resolution activities."""

from __future__ import annotations

from temporalio import activity

from harborrag_runtime.temporal.activities.schemas import ActivityTelemetryContext
from harborrag_runtime.temporal.activities.telemetry import record_activity
from harborrag_runtime.temporal.dependencies import RuntimeDependencies
from harborrag_runtime.temporal.schemas import (
    ArtifactActivityInput,
    ArtifactActivityResult,
    ArtifactStage,
    HeartbeatProgress,
    ReconciliationInput,
    ReconciliationResult,
    ResolutionDecision,
    ResolutionReceipt,
)


class MaintenanceActivities:
    def __init__(self, dependencies: RuntimeDependencies) -> None:
        self._dependencies = dependencies

    @activity.defn(name="harborrag.finalize_artifact")
    async def finalize_artifact(
        self,
        request: ArtifactActivityInput,
    ) -> ArtifactActivityResult:
        existing = await self._dependencies.state.completed_stage(
            request,
            ArtifactStage.FINALIZE,
        )
        if existing is not None:
            return existing
        activity.heartbeat(HeartbeatProgress(stage="finalize", completed=0, total=1))
        result = await self._dependencies.state.finalize(request)
        activity.heartbeat(HeartbeatProgress(stage="finalize", completed=1, total=1))
        completed = await self._dependencies.state.complete_stage(request, result)
        record_activity(
            self._dependencies,
            "runtime.artifact.finalized",
            ActivityTelemetryContext(
                run_id=request.run_id,
                artifact_id=request.state.artifact.artifact_id,
                artifact_revision_id=request.state.artifact_revision_id,
                generation_id=request.state.generation_id,
            ),
        )
        return completed

    @activity.defn(name="harborrag.reconcile_ingestion")
    async def reconcile_ingestion(
        self,
        request: ReconciliationInput,
    ) -> ReconciliationResult:
        activity.heartbeat(HeartbeatProgress(stage="reconcile", completed=0, total=1))
        result = await self._dependencies.state.reconcile(request)
        activity.heartbeat(
            HeartbeatProgress(
                stage="reconcile",
                completed=1,
                total=1,
                checkpoint_ref=result.reconciliation_ref,
            )
        )
        record_activity(
            self._dependencies,
            "runtime.ingestion.reconciled",
            ActivityTelemetryContext(
                run_id=request.run_id,
                generation_id=request.generation_id,
                measurements={
                    "processed": request.progress.processed,
                    "failed": request.progress.failed,
                    "quarantined": request.progress.quarantined,
                },
            ),
        )
        return result

    @activity.defn(name="harborrag.apply_resolution")
    async def apply_resolution(
        self,
        request: ArtifactActivityInput,
        decision: ResolutionDecision,
    ) -> ResolutionReceipt:
        activity.heartbeat(HeartbeatProgress(stage="resolution", completed=0, total=1))
        receipt = await self._dependencies.state.apply_resolution(request, decision)
        activity.heartbeat(
            HeartbeatProgress(
                stage="resolution",
                completed=1,
                total=1,
                checkpoint_ref=receipt.decision_ref,
            )
        )
        record_activity(
            self._dependencies,
            "runtime.resolution.applied",
            ActivityTelemetryContext(
                run_id=request.run_id,
                artifact_id=request.state.artifact.artifact_id,
                artifact_revision_id=request.state.artifact_revision_id,
                generation_id=request.state.generation_id,
            ),
        )
        return receipt
