from __future__ import annotations

import pytest

from harborrag_adapters.repositories.object_store.memory import MemoryObjectStore
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord
from harborrag_core.schemas.state import WorkflowState
from harborrag_core.schemas.storage import (
    HealthStatus,
    RepositoryHealth,
    StorageFamily,
)
from harborrag_engine.ingestion import DocumentNormalizer
from harborrag_engine.ingestion.indexing import (
    GenerationActivationPlan,
    IndexingConfig,
    IndexingDiagnostics,
    IndexingResult,
    IndexingStatus,
)
from harborrag_runtime.temporal.ingestionstate import (
    IngestionObjectRepository,
    RepositoryRuntimeIngestionState,
)
from harborrag_runtime.temporal.schemas import (
    ArtifactActivityInput,
    ArtifactStage,
    ArtifactStageState,
    ArtifactStatus,
    DiscoveryInput,
)


class RecordingActivator:
    def __init__(self) -> None:
        self.requests = []

    async def activate(self, request) -> None:
        self.requests.append(request)


class MemoryStateStore:
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


@pytest.mark.asyncio
async def test_repository_state_survives_new_service_instance() -> None:
    backend = MemoryStateBackend()
    store = MemoryObjectStore()
    await store.connect()
    try:
        objects = IngestionObjectRepository(store)
        config = IndexingConfig("embed", 3, "chunks", "graph")
        first = RepositoryRuntimeIngestionState(backend, objects, config)
        discovery = DiscoveryInput(
            run_id="run-1",
            tenant_id="tenant-1",
            manifest_id="manifest-1",
            connector_name="local-docs",
            cursor=None,
            page_size=10,
        )
        await first.initialize_run(discovery)
        source = SourceRecord(
            id="document-1",
            source_type="local",
            locator="docs/readme.md",
            checksum="revision-1",
        )
        artifact = await first.persist_discovered(discovery, source)
        await first.save_discovery_progress(discovery, (artifact,), None, done=True)

        second = RepositoryRuntimeIngestionState(backend, objects, config)
        progress = await second.discovery_progress(discovery)
        assert progress is not None and progress.done
        assert progress.artifacts == (artifact,)
        assert await second.load_source(artifact.source_ref) == source

        request = ArtifactActivityInput(
            "run-1",
            "tenant-1",
            "manifest-1",
            ArtifactStageState(artifact, "generation-1"),
        )
        preflight = await second.preflight(request)
        assert preflight.state.artifact_revision_id == "revision-1"
        assert preflight.state.stage is ArtifactStage.FETCH
        completed = await second.complete_stage(request, preflight)
        assert await second.completed_stage(request, ArtifactStage.PREFLIGHT) == completed

        raw = RawDocument(
            "document-1",
            "local",
            b"# Guide",
            "text/markdown",
            {"title": "Guide"},
        )
        snapshot_ref = await second.persist_snapshot(request, raw)
        loaded_raw = await second.load_snapshot(snapshot_ref)
        assert loaded_raw.content == b"# Guide"

        parsed = ParsedDocument(
            content="Guide",
            parser_name="markdown",
            elements=[DocumentElement("heading-1", "heading", "Guide")],
        )
        document = DocumentNormalizer().normalize(raw, parsed)
        parsed_ref = await second.persist_parsed_document(request, parsed, document)
        loaded_document = await second.load_parsed_document(parsed_ref)
        assert loaded_document.content == document.content
        assert (await second.health())["ready"] is True
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_finalize_activates_and_configuration_changes_bypass_unchanged() -> None:
    backend = MemoryStateBackend()
    store = MemoryObjectStore()
    await store.connect()
    try:
        objects = IngestionObjectRepository(store)
        config = IndexingConfig("embed", 3, "chunks", "graph")
        activator = RecordingActivator()
        state = RepositoryRuntimeIngestionState(
            backend,
            objects,
            config,
            activator,  # type: ignore[arg-type]
        )
        discovery = DiscoveryInput(
            "run-1",
            "tenant-1",
            "manifest-1",
            "local-docs",
            None,
            10,
        )
        artifact = await state.persist_discovered(
            discovery,
            SourceRecord(
                id="document-1",
                source_type="local",
                locator="docs/readme.md",
                checksum="revision-1",
            ),
        )
        initial = ArtifactActivityInput(
            "run-1",
            "tenant-1",
            "manifest-1",
            ArtifactStageState(artifact, "generation-1"),
        )
        preflight = await state.preflight(initial)
        final_request = ArtifactActivityInput(
            "run-1",
            "tenant-1",
            "manifest-1",
            ArtifactStageState(
                artifact,
                "generation-1",
                stage=ArtifactStage.FINALIZE,
                artifact_revision_id=preflight.state.artifact_revision_id,
                chunking_result_ref="harbor-object://chunking",
            ),
        )
        result = IndexingResult(
            artifact_id="document-1",
            artifact_revision_id="revision-1",
            generation_id="generation-1",
            status=IndexingStatus.SUCCEEDED,
            vector_valid=True,
            graph_valid=True,
            validation_errors=(),
            diagnostics=IndexingDiagnostics(
                new_chunks=0,
                unchanged_chunks=0,
                changed_chunks=0,
                removed_chunks=0,
                reembedded_chunks=0,
                embedded_chunks=0,
                embedding_batches=0,
                vector_upserts=0,
                vector_retentions=0,
                vector_retirements=0,
                vector_deletions=0,
                vector_tombstones=0,
                graph_nodes=0,
                graph_edges=0,
            ),
            activation=GenerationActivationPlan(
                "document-1",
                "generation-1",
                None,
                "chunks",
                (),
                (),
                (),
                (),
            ),
        )
        indexing_ref = await state.persist_indexing_result(final_request, result)
        final_request = ArtifactActivityInput(
            "run-1",
            "tenant-1",
            "manifest-1",
            ArtifactStageState(
                artifact,
                "generation-1",
                stage=ArtifactStage.FINALIZE,
                artifact_revision_id="revision-1",
                chunking_result_ref="harbor-object://chunking",
                indexing_result_ref=indexing_ref,
            ),
        )

        finalized = await state.finalize(final_request)

        assert finalized.status is ArtifactStatus.SUCCEEDED
        assert len(activator.requests) == 1
        unchanged = await state.preflight(
            ArtifactActivityInput(
                "run-2",
                "tenant-1",
                "manifest-2",
                ArtifactStageState(artifact, "generation-2"),
            )
        )
        assert unchanged.status is ArtifactStatus.UNCHANGED

        changed = RepositoryRuntimeIngestionState(
            backend,
            objects,
            IndexingConfig("embed", 3, "other-collection", "graph"),
        )
        changed_preflight = await changed.preflight(
            ArtifactActivityInput(
                "run-3",
                "tenant-1",
                "manifest-3",
                ArtifactStageState(artifact, "generation-3"),
            )
        )
        assert changed_preflight.status is ArtifactStatus.RUNNING
    finally:
        await store.close()
