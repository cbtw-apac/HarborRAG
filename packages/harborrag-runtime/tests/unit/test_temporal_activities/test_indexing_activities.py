from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from temporalio.testing import ActivityEnvironment

from harborrag_engine.ingestion.indexing.schemas import (
    IndexingFailure,
    IndexingStatus,
)
from harborrag_runtime.temporal.activities.indexing import IndexingActivities
from harborrag_runtime.temporal.schemas import (
    ArtifactActivityInput,
    ArtifactReference,
    ArtifactStage,
    ArtifactStageState,
)


@pytest.mark.asyncio
async def test_failed_engine_index_result_is_persisted_then_retried() -> None:
    artifact = ArtifactReference("a", "source://a", "local", "local")
    stage_state = ArtifactStageState(
        artifact=artifact,
        generation_id="generation-1",
        stage=ArtifactStage.INDEX,
        artifact_revision_id="revision-1",
        chunking_result_ref="chunks://a",
    )
    request = ArtifactActivityInput("run-1", "tenant-1", "manifest-1", stage_state)
    engine_result = SimpleNamespace(
        status=IndexingStatus.FAILED,
        validation_errors=("vector provider unavailable",),
    )
    state = SimpleNamespace(
        completed_stage=AsyncMock(return_value=None),
        load_chunking_result=AsyncMock(return_value=SimpleNamespace()),
        indexing_request=AsyncMock(return_value=SimpleNamespace()),
        persist_indexing_result=AsyncMock(return_value="index://failed"),
    )
    dependencies = SimpleNamespace(
        state=state,
        indexer=SimpleNamespace(index=AsyncMock(return_value=engine_result)),
    )

    with pytest.raises(RuntimeError, match="retryable indexing provider failure"):
        await ActivityEnvironment().run(
            IndexingActivities(dependencies).index_artifact,
            request,
        )

    state.persist_indexing_result.assert_awaited_once_with(request, engine_result)


@pytest.mark.asyncio
async def test_retryable_partial_index_result_is_checkpointed_then_retried() -> None:
    artifact = ArtifactReference("a", "source://a", "local", "local")
    request = ArtifactActivityInput(
        "run-1",
        "tenant-1",
        "manifest-1",
        ArtifactStageState(
            artifact=artifact,
            generation_id="generation-1",
            stage=ArtifactStage.INDEX,
            artifact_revision_id="revision-1",
            chunking_result_ref="chunks://a",
        ),
    )
    engine_result = SimpleNamespace(
        status=IndexingStatus.PARTIAL,
        vector_valid=True,
        graph_valid=False,
        validation_errors=("graph: TimeoutError",),
        failures=(IndexingFailure("graph", "TimeoutError", True),),
    )
    state = SimpleNamespace(
        completed_stage=AsyncMock(return_value=None),
        load_chunking_result=AsyncMock(return_value=SimpleNamespace()),
        indexing_request=AsyncMock(return_value=SimpleNamespace()),
        persist_indexing_result=AsyncMock(return_value="index://partial"),
    )
    dependencies = SimpleNamespace(
        state=state,
        indexer=SimpleNamespace(index=AsyncMock(return_value=engine_result)),
    )

    with pytest.raises(RuntimeError, match="retryable indexing provider failure"):
        await ActivityEnvironment().run(
            IndexingActivities(dependencies).index_artifact,
            request,
        )

    state.persist_indexing_result.assert_awaited_once_with(request, engine_result)
