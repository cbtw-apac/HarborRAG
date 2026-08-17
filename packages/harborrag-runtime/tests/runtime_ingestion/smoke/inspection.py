"""Independent Postgres, MinIO, Qdrant, and FalkorDB smoke assertions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from projection_inspection import (
    ChunkObservation,
    GraphObservation,
    ProjectionStores,
    inspect_projections,
)

from harborrag_core.ingestion import (
    TaskDocumentResult,
)
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_engine.ingestion import (
    EVIDENCE_INDEX,
)
from harborrag_runtime.composition.resources import (
    build_ingestion_control,
    build_knowledge_graph,
    build_object_store,
    build_vector_repository,
)
from harborrag_runtime.config.settings import RuntimeSettings


@dataclass(frozen=True, slots=True)
class StoreObservation:
    documents: int
    document_ids: tuple[str, ...]
    versions: tuple[str, ...]
    artifact_keys: tuple[str, ...]
    chunks: tuple[ChunkObservation, ...]
    graphs: tuple[GraphObservation, ...]

    @property
    def evidence_chunks(self) -> int:
        return sum(chunk.collection == EVIDENCE_INDEX for chunk in self.chunks)


async def inspect_stores(
    settings: RuntimeSettings,
    *,
    tenant_id: str,
    task_id: str,
) -> StoreObservation:
    """Cross-check authoritative task results against all derived stores."""

    control = build_ingestion_control(settings)
    objects = build_object_store(settings)
    vectors = build_vector_repository(settings)
    graph = build_knowledge_graph(settings)
    connected = []
    try:
        for resource in (control, objects, vectors, graph):
            await resource.connect()
            connected.append(resource)
        results = await control.tasks.document_results(task_id)
        _assert_task_results(results)
        pending_cleanup = await control.reliability.pending_cleanup_jobs(
            limit=1_000,
            document_ids=tuple(str(result.document_id) for result in results),
        )
        if pending_cleanup:
            raise AssertionError("smoke documents retain pending projection cleanup jobs")
        snapshots = await asyncio.gather(
            *(
                control.document_versions.active_snapshot(str(result.document_id))
                for result in results
            )
        )
        if any(snapshot is None for snapshot in snapshots):
            raise AssertionError("every smoke document must be active")
        active_snapshots = tuple(snapshot for snapshot in snapshots if snapshot is not None)
        document_ids = tuple(str(result.document_id) for result in results)
        versions = tuple(str(snapshot.document_version_id) for snapshot in active_snapshots)
        manifests = await asyncio.gather(
            *(
                control.reliability.projection_manifest(str(snapshot.document_version_id))
                for snapshot in snapshots
                if snapshot is not None
            )
        )
        if any(manifest is None for manifest in manifests):
            raise AssertionError("every smoke document must have a projection manifest")
        artifact_keys = tuple(
            sorted(
                (
                    *(
                        reference.key
                        for snapshot in snapshots
                        if snapshot is not None
                        for reference in (
                            snapshot.raw_artifact,
                            snapshot.raw_metadata_artifact,
                            snapshot.canonical_artifact,
                            snapshot.chunk_artifact,
                            snapshot.chunk_index_artifact,
                            snapshot.relation_artifact,
                            snapshot.representation_artifact,
                        )
                        if reference is not None
                    ),
                    *(
                        reference.key
                        for manifest in manifests
                        if manifest is not None
                        for reference in (
                            *manifest.table_artifacts,
                            manifest.comment_artifact,
                            manifest.vector_artifact,
                            manifest.graph_artifact,
                        )
                        if reference is not None
                    ),
                )
            )
        )
        _assert_artifact_layout(artifact_keys, len(results))
        context = StorageOperationContext.system(tenant_id=tenant_id)
        chunks, graphs = await inspect_projections(
            stores=ProjectionStores(
                vectors=vectors,
                graph=graph,
                objects=objects,
            ),
            documents=tuple(zip(document_ids, versions, strict=True)),
            versions=frozenset(versions),
            context=context,
        )
        return StoreObservation(
            documents=len(results),
            document_ids=document_ids,
            versions=versions,
            artifact_keys=artifact_keys,
            chunks=chunks,
            graphs=graphs,
        )
    finally:
        await asyncio.gather(
            *(resource.close() for resource in reversed(connected)),
            return_exceptions=True,
        )


def _assert_task_results(
    results: tuple[TaskDocumentResult, ...],
) -> None:
    if len(results) != 2:
        raise AssertionError(f"smoke task expected 2 document results, found {len(results)}")
    if any(result.status == "failed" for result in results):
        raise AssertionError("smoke task contains a failed document")
    if any(result.document_version_id is None for result in results):
        raise AssertionError("smoke document result has no version")


def _assert_artifact_layout(
    keys: tuple[str, ...],
    document_count: int,
) -> None:
    required_prefixes = (
        "raw/",
        "canonical/",
        "comments/",
        "chunks/",
        "relations/",
        "representations/",
        "projections/",
    )
    for prefix in required_prefixes:
        if sum(key.startswith(prefix) for key in keys) < document_count:
            raise AssertionError(f"immutable artifact layout is missing {prefix}")
    if not any(key.startswith("tables/") for key in keys):
        raise AssertionError("immutable artifact layout is missing table Parquet")
