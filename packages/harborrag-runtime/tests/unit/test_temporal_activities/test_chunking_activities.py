from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from temporalio.testing import ActivityEnvironment

from harborrag_runtime.temporal.activities.chunking import ChunkingActivities
from harborrag_runtime.temporal.schemas import (
    ArtifactActivityInput,
    ArtifactActivityResult,
    ArtifactReference,
    ArtifactStage,
    ArtifactStageState,
    ArtifactStatus,
)

from .fakes import Observer


@pytest.mark.asyncio
async def test_chunk_activity_calls_engine_and_reuses_completed_stage() -> None:
    artifact = ArtifactReference("a", "source://a", "local", "local")
    stage_state = ArtifactStageState(
        artifact=artifact,
        generation_id="generation-1",
        stage=ArtifactStage.CHUNK,
        artifact_revision_id="revision-1",
        parsed_document_ref="parsed://a",
    )
    request = ArtifactActivityInput("run-1", "tenant-1", "manifest-1", stage_state)
    completed = ArtifactActivityResult(
        status=ArtifactStatus.RUNNING,
        state=stage_state,
    )
    state = SimpleNamespace(completed_stage=AsyncMock(return_value=completed))
    chunker = SimpleNamespace(chunk=Mock())
    dependencies = SimpleNamespace(state=state, chunker=chunker)

    result = await ActivityEnvironment().run(
        ChunkingActivities(dependencies).chunk_artifact,
        request,
    )

    assert result is completed
    chunker.chunk.assert_not_called()


@pytest.mark.asyncio
async def test_chunk_activity_delegates_to_engine_and_persists_before_reference(
    monkeypatch,
) -> None:
    async def direct(call, *args):
        return call(*args)

    monkeypatch.setattr(
        "harborrag_runtime.temporal.activities.processing.asyncio.to_thread",
        direct,
    )
    artifact = ArtifactReference("a", "source://a", "local", "local")
    stage_state = ArtifactStageState(
        artifact=artifact,
        generation_id="generation-1",
        stage=ArtifactStage.CHUNK,
        artifact_revision_id="revision-1",
        parsed_document_ref="parsed://a",
    )
    request = ArtifactActivityInput("run-1", "tenant-1", "manifest-1", stage_state)
    engine_result = SimpleNamespace(manifest=SimpleNamespace(total_chunk_count=3))
    document = SimpleNamespace(
        id="a",
        content_type="text/plain",
        provenance=SimpleNamespace(source="local", extra={}),
    )
    state = SimpleNamespace(
        completed_stage=AsyncMock(return_value=None),
        load_parsed_document=AsyncMock(return_value=document),
        persist_chunking_result=AsyncMock(return_value="chunks://a"),
        complete_stage=AsyncMock(side_effect=lambda request, result: result),
    )
    chunker = SimpleNamespace(chunk=Mock(return_value=engine_result))
    persistence = SimpleNamespace(persist=AsyncMock())
    dependencies = SimpleNamespace(
        state=state,
        chunker=chunker,
        chunk_persistence=persistence,
        observer=Observer(),
    )

    result = await ActivityEnvironment().run(
        ChunkingActivities(dependencies).chunk_artifact,
        request,
    )

    chunker.chunk.assert_called_once()
    persistence.persist.assert_awaited_once_with(engine_result)
    state.persist_chunking_result.assert_awaited_once_with(request, engine_result)
    assert result.state.chunking_result_ref == "chunks://a"
