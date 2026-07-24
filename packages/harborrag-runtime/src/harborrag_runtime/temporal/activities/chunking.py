"""Chunking activity that delegates all chunk policy to the engine."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from temporalio import activity

from harborrag_engine.ingestion.chunking.schemas import ChunkingRequest
from harborrag_runtime.temporal.activities.telemetry import record_activity
from harborrag_runtime.temporal.dependencies import RuntimeDependencies
from harborrag_runtime.temporal.schemas import (
    ArtifactActivityInput,
    ArtifactActivityResult,
    ArtifactStage,
    ArtifactStatus,
    HeartbeatProgress,
)


class ChunkingActivities:
    def __init__(self, dependencies: RuntimeDependencies) -> None:
        self._dependencies = dependencies

    @activity.defn(name="harborrag.chunk_artifact")
    async def chunk_artifact(
        self,
        request: ArtifactActivityInput,
    ) -> ArtifactActivityResult:
        existing = await self._dependencies.state.completed_stage(
            request,
            ArtifactStage.CHUNK,
        )
        if existing is not None:
            return existing
        parsed_ref = request.state.parsed_document_ref
        revision_id = request.state.artifact_revision_id
        if parsed_ref is None or revision_id is None:
            raise ValueError("chunk activity requires parsed document and artifact revision refs")

        activity.heartbeat(HeartbeatProgress(stage="chunk", completed=0, total=1))
        document = await self._dependencies.state.load_parsed_document(parsed_ref)
        chunking_request = ChunkingRequest(
            tenant_id=request.tenant_id,
            artifact_id=request.state.artifact.artifact_id,
            artifact_revision_id=revision_id,
            document=document,
            source_kind=request.state.artifact.source_kind,
        )
        result = await asyncio.to_thread(self._dependencies.chunker.chunk, chunking_request)
        await self._dependencies.chunk_persistence.persist(result)
        result_ref = await self._dependencies.state.persist_chunking_result(request, result)
        state = replace(
            request.state,
            stage=ArtifactStage.INDEX,
            chunking_result_ref=result_ref,
        )
        output = ArtifactActivityResult(status=ArtifactStatus.RUNNING, state=state)
        activity.heartbeat(
            HeartbeatProgress(
                stage="chunk",
                completed=result.manifest.total_chunk_count,
                total=result.manifest.total_chunk_count,
                checkpoint_ref=result_ref,
            )
        )
        completed = await self._dependencies.state.complete_stage(request, output)
        record_activity(
            self._dependencies,
            "runtime.chunking.completed",
            run_id=request.run_id,
            artifact_id=request.state.artifact.artifact_id,
            artifact_revision_id=revision_id,
            generation_id=request.state.generation_id,
            measurements={"chunks": result.manifest.total_chunk_count},
        )
        return completed
