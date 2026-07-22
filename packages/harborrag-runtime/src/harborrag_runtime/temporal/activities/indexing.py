"""Indexing and validation activities delegated to engine/state services."""

from __future__ import annotations

from dataclasses import replace

from harborrag_engine.ingestion.indexing.schemas import IndexingStatus
from temporalio import activity

from harborrag_runtime.temporal.activities.telemetry import record_activity
from harborrag_runtime.temporal.dependencies import RuntimeDependencies
from harborrag_runtime.temporal.models import (
    ArtifactActivityInput,
    ArtifactActivityResult,
    ArtifactStage,
    ArtifactStatus,
    HeartbeatProgress,
)


class IndexingActivities:
    def __init__(self, dependencies: RuntimeDependencies) -> None:
        self._dependencies = dependencies

    @activity.defn(name="harborrag.index_artifact")
    async def index_artifact(
        self,
        request: ArtifactActivityInput,
    ) -> ArtifactActivityResult:
        existing = await self._dependencies.state.completed_stage(
            request,
            ArtifactStage.INDEX,
        )
        if existing is not None:
            return existing
        chunking_ref = request.state.chunking_result_ref
        if chunking_ref is None:
            raise ValueError("index activity requires chunking_result_ref")

        activity.heartbeat(HeartbeatProgress(stage="index", completed=0, total=1))
        chunking = await self._dependencies.state.load_chunking_result(chunking_ref)
        index_request = await self._dependencies.state.indexing_request(request, chunking)
        result = await self._dependencies.indexer.index(index_request)
        result_ref = await self._dependencies.state.persist_indexing_result(request, result)
        if result.status is IndexingStatus.FAILED:
            activity.heartbeat(
                HeartbeatProgress(
                    stage="index",
                    completed=1,
                    total=1,
                    checkpoint_ref=result_ref,
                )
            )
            raise RuntimeError(
                f"indexing generation failed; persisted result reference: {result_ref}"
            )
        status = (
            ArtifactStatus.RUNNING
            if result.status is IndexingStatus.SUCCEEDED
            else ArtifactStatus.QUARANTINED
        )
        state = replace(
            request.state,
            stage=ArtifactStage.VALIDATE,
            indexing_result_ref=result_ref,
        )
        output = ArtifactActivityResult(
            status=status,
            state=state,
            error_type=(None if status is ArtifactStatus.RUNNING else result.status.value),
            error_message=(
                None if status is ArtifactStatus.RUNNING else "; ".join(result.validation_errors)
            ),
            retryable=False,
        )
        activity.heartbeat(
            HeartbeatProgress(
                stage="index",
                completed=1,
                total=1,
                checkpoint_ref=result_ref,
            )
        )
        completed = await self._dependencies.state.complete_stage(request, output)
        record_activity(
            self._dependencies,
            "runtime.indexing.completed",
            run_id=request.run_id,
            artifact_id=request.state.artifact.artifact_id,
            artifact_revision_id=request.state.artifact_revision_id,
            generation_id=request.state.generation_id,
        )
        return completed

    @activity.defn(name="harborrag.validate_artifact")
    async def validate_artifact(
        self,
        request: ArtifactActivityInput,
    ) -> ArtifactActivityResult:
        existing = await self._dependencies.state.completed_stage(
            request,
            ArtifactStage.VALIDATE,
        )
        if existing is not None:
            return existing
        activity.heartbeat(HeartbeatProgress(stage="validate", completed=0, total=1))
        result = await self._dependencies.state.validate(request)
        activity.heartbeat(HeartbeatProgress(stage="validate", completed=1, total=1))
        completed = await self._dependencies.state.complete_stage(request, result)
        record_activity(
            self._dependencies,
            "runtime.validation.completed",
            run_id=request.run_id,
            artifact_id=request.state.artifact.artifact_id,
            artifact_revision_id=request.state.artifact_revision_id,
            generation_id=request.state.generation_id,
        )
        return completed
