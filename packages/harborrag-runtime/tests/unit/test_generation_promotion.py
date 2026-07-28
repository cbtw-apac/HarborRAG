"""Optimistic reservation and two-phase promotion of an artifact generation.

`GenerationPromotionMixin` is the concurrency protocol that keeps two workers
from activating different generations of the same artifact. The refusal and
conflict-retry branches carry the whole guarantee, and none of them were
exercised: the existing state suite only walks the uncontended path.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from harborrag_adapters.repositories.errors import (
    HarborStorageCheckpointConflictError,
    StorageErrorContext,
)
from harborrag_adapters.repositories.object_store.memory import MemoryObjectStore
from harborrag_core.domain.source import SourceRecord
from harborrag_core.schemas.state import WorkflowState
from harborrag_core.schemas.storage import (
    HealthStatus,
    RepositoryHealth,
    StorageFamily,
)
from harborrag_engine.ingestion.indexing import (
    GenerationActivationPlan,
    IndexingConfig,
    IndexingDiagnostics,
    IndexingResult,
    IndexingStatus,
)
from harborrag_runtime.temporal.artifact_objects import IngestionObjectRepository
from harborrag_runtime.temporal.ingestionstate import RepositoryRuntimeIngestionState
from harborrag_runtime.temporal.schemas import (
    ArtifactActivityInput,
    ArtifactStageState,
    DiscoveryInput,
)

TENANT = "tenant-1"
RUN = "run-1"


class MemoryStateStore:
    """Version-checked in-memory workflow state, mirroring the real contract."""

    def __init__(self) -> None:
        self.values: dict[str, WorkflowState] = {}

    async def create(self, state, *, context):
        del context
        self.values[str(state.workflow_id)] = state
        return state

    async def get(self, workflow_id, *, context):
        del context
        return self.values.get(str(workflow_id))

    async def save(self, state, *, expected_version, context):
        del context
        saved = state.model_copy(update={"version": expected_version + 1})
        self.values[str(state.workflow_id)] = saved
        return saved


class MemoryStateBackend:
    def __init__(self) -> None:
        self.state = MemoryStateStore()

    async def health(self) -> RepositoryHealth:
        return RepositoryHealth(
            family=StorageFamily.STATE,
            backend="memory",
            instance_name="test",
            status=HealthStatus.HEALTHY,
        )


@pytest_asyncio.fixture
async def state():
    store = MemoryObjectStore()
    await store.connect()
    try:
        backend = MemoryStateBackend()
        objects = IngestionObjectRepository(store)
        service = RepositoryRuntimeIngestionState(
            backend,
            objects,
            IndexingConfig("embed", 3, "chunks", "graph"),
        )
        discovery = DiscoveryInput(
            run_id=RUN,
            tenant_id=TENANT,
            manifest_id="manifest-1",
            connector_name="local-docs",
            cursor=None,
            page_size=10,
        )
        artifact = await service.persist_discovered(
            discovery,
            SourceRecord(
                id="document-1",
                source_type="local",
                locator="docs/readme.md",
                checksum="revision-1",
            ),
        )
        yield service, artifact
    finally:
        await store.close()


def _request(artifact, generation_id: str) -> ArtifactActivityInput:
    return ArtifactActivityInput(
        RUN,
        TENANT,
        "manifest-1",
        ArtifactStageState(
            artifact,
            generation_id,
            artifact_revision_id="revision-1",
        ),
    )


def _indexing(previous_generation_id: str | None) -> IndexingResult:
    return IndexingResult(
        artifact_id="document-1",
        artifact_revision_id="revision-1",
        generation_id="generation-1",
        status=IndexingStatus.SUCCEEDED,
        vector_valid=True,
        graph_valid=True,
        validation_errors=(),
        diagnostics=IndexingDiagnostics(
            new_chunks=1,
            unchanged_chunks=0,
            changed_chunks=0,
            removed_chunks=0,
            reembedded_chunks=0,
            embedded_chunks=1,
            embedding_batches=1,
            vector_upserts=1,
            vector_retentions=0,
            vector_retirements=0,
            vector_deletions=0,
            vector_tombstones=0,
            graph_nodes=1,
            graph_edges=0,
        ),
        activation=GenerationActivationPlan(
            artifact_id="document-1",
            generation_id="generation-1",
            previous_generation_id=previous_generation_id,
            vector_collection="chunks",
            activate_vector_ids=("v1",),
            retire_vector_ids=(),
            delete_vector_ids=(),
            tombstone_vector_ids=(),
        ),
    )


# --------------------------------------------------------------------------
# Reservation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reservation_is_idempotent_for_the_same_generation(state) -> None:
    service, artifact = state
    request = _request(artifact, "generation-1")

    await service._reserve_generation(request, "revision-1")
    await service._reserve_generation(request, "revision-1")

    stored = await service._states.get(
        service._active_id(request),
        context=service._context(TENANT, RUN),
    )
    assert stored is not None
    assert stored.payload["pending_generation_id"] == "generation-1"


@pytest.mark.asyncio
async def test_reservation_refuses_while_another_generation_is_promoting(state) -> None:
    service, artifact = state
    first = _request(artifact, "generation-1")
    await service._reserve_generation(first, "revision-1")
    assert await service._begin_promotion(first, _indexing(None)) is True

    second = _request(artifact, "generation-2")
    with pytest.raises(RuntimeError, match="another generation is being promoted"):
        await service._reserve_generation(second, "revision-1")


@pytest.mark.asyncio
async def test_reservation_gives_up_after_repeated_write_conflicts(state) -> None:
    """A permanently contended state store must fail loudly, not spin."""
    service, artifact = state
    request = _request(artifact, "generation-1")
    await service._reserve_generation(request, "revision-1")

    conflict_context = StorageErrorContext(
        family=StorageFamily.STATE,
        backend="memory",
        instance_name="test",
        operation="save",
        retryable=True,
    )

    async def _always_conflict(*args: object, **kwargs: object) -> None:
        raise HarborStorageCheckpointConflictError("contended", context=conflict_context)

    service._states.save = _always_conflict  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="could not reserve artifact generation"):
        await service._reserve_generation(request, "revision-1")


# --------------------------------------------------------------------------
# Begin promotion
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_begin_promotion_requires_a_reservation(state) -> None:
    service, artifact = state
    request = _request(artifact, "generation-1")

    with pytest.raises(ValueError, match="was not reserved during preflight"):
        await service._begin_promotion(request, _indexing(None))


@pytest.mark.asyncio
async def test_begin_promotion_declines_a_superseded_generation(state) -> None:
    service, artifact = state
    await service._reserve_generation(_request(artifact, "generation-2"), "revision-1")

    stale = _request(artifact, "generation-1")

    assert await service._begin_promotion(stale, _indexing(None)) is False


@pytest.mark.asyncio
async def test_begin_promotion_declines_when_the_active_generation_moved(state) -> None:
    """The plan's expected predecessor must still be the active generation."""
    service, artifact = state
    request = _request(artifact, "generation-1")
    await service._reserve_generation(request, "revision-1")

    assert await service._begin_promotion(request, _indexing("generation-0")) is False


@pytest.mark.asyncio
async def test_begin_promotion_is_idempotent(state) -> None:
    service, artifact = state
    request = _request(artifact, "generation-1")
    await service._reserve_generation(request, "revision-1")

    assert await service._begin_promotion(request, _indexing(None)) is True
    assert await service._begin_promotion(request, _indexing(None)) is True


# --------------------------------------------------------------------------
# Finish promotion
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finish_promotion_activates_and_clears_pending_keys(state) -> None:
    service, artifact = state
    request = _request(artifact, "generation-1")
    await service._reserve_generation(request, "revision-1")
    await service._begin_promotion(request, _indexing(None))

    assert await service._finish_promotion(request) is True

    stored = await service._states.get(
        service._active_id(request),
        context=service._context(TENANT, RUN),
    )
    assert stored is not None
    assert stored.payload["active_generation_id"] == "generation-1"
    assert stored.payload["active_revision_id"] == "revision-1"
    for key in (
        "pending_generation_id",
        "pending_revision_id",
        "promotion_generation_id",
        "promotion_revision_id",
    ):
        assert key not in stored.payload


@pytest.mark.asyncio
async def test_finish_promotion_declines_without_a_matching_promotion(state) -> None:
    service, artifact = state
    request = _request(artifact, "generation-1")
    await service._reserve_generation(request, "revision-1")

    # Reserved but never promoted, so there is nothing to finish.
    assert await service._finish_promotion(request) is False


@pytest.mark.asyncio
async def test_finish_promotion_requires_existing_state(state) -> None:
    service, artifact = state
    request = _request(artifact, "generation-1")

    with pytest.raises(ValueError, match="promotion state disappeared"):
        await service._finish_promotion(request)
