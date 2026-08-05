from __future__ import annotations

import json

import pytest

from harborrag_adapters.repositories.object_store import (
    ARTIFACT_BUCKET,
    CanonicalDocumentArtifactRepository,
    ImmutableArtifact,
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
    IngestionArtifactLayout,
    MemoryObjectStore,
    ProjectionArtifactRepository,
)
from harborrag_core.chunking import (
    ChunkKind,
    CitationLocator,
    ConnectorType,
    RecordKind,
    RelationType,
)
from harborrag_core.domain import Document, DocumentElement, DocumentProvenance
from harborrag_core.ingestion import (
    GraphEdgeRecord,
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeNodeKind,
    VectorEvidenceRecord,
    VectorPayload,
)
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_core.schemas.vector import SparseVector


def context() -> StorageOperationContext:
    return StorageOperationContext.system(tenant_id="tenant-1")


def canonical_document() -> Document:
    return Document(
        id="document-1",
        title="Release Guide",
        content=[
            DocumentElement(
                id="paragraph-1",
                type="paragraph",
                content="The timeout is 30 seconds.",
            )
        ],
        content_type="confluence_page",
        provenance=DocumentProvenance(
            source="confluence",
            record_id="page-1",
        ),
    )


@pytest.mark.asyncio
async def test_canonical_repository_round_trips_an_immutable_document() -> None:
    store = MemoryObjectStore()
    async with store:
        repository = CanonicalDocumentArtifactRepository(
            ImmutableArtifactWriter(store),
            ImmutableArtifactReader(store),
        )
        reference = await repository.put(
            document_id="document-1",
            document_version_id="version-1",
            document=canonical_document(),
            context=context(),
        )
        replay = await repository.put(
            document_id="document-1",
            document_version_id="version-1",
            document=canonical_document(),
            context=context(),
        )

        assert replay == reference
        assert await repository.get(reference, context=context()) == canonical_document()


@pytest.mark.asyncio
async def test_projection_repository_round_trips_replayable_batches() -> None:
    store = MemoryObjectStore()
    async with store:
        repository = ProjectionArtifactRepository(
            ImmutableArtifactWriter(store),
            ImmutableArtifactReader(store),
        )
        node = GraphNodeRecord(
            node_key="node-1",
            node_kind=KnowledgeNodeKind.DOCUMENT_VERSION,
            entity_type=GraphEntityType.DOCUMENT_VERSION,
            logical_id="document-1",
            ownership_scope=GraphOwnershipScope.DOCUMENT_VERSION,
            owner_id="tenant-1",
            document_id="document-1",
            document_version_id="version-1",
            source_scope_id="scope-1",
            title="Release Guide",
        )
        relation = GraphEdgeRecord(
            relation_id="relation-1",
            relation_type=RelationType.CONTAINS,
            source_node_key="node-1",
            target_node_key="node-2",
            ownership_scope=GraphOwnershipScope.DOCUMENT_VERSION,
            owner_id="tenant-1",
            source_scope_id="scope-1",
            document_id="document-1",
            document_version_id="version-1",
            source_relation_version="graph-v1",
            source_explicit=False,
        )
        point = VectorEvidenceRecord(
            point_id="00000000-0000-5000-8000-000000000001",
            tenant_id="tenant-1",
            dense_vector=(0.1, 0.2),
            sparse_vector=SparseVector(indices=[1], values=[1.0]),
            payload=VectorPayload(
                chunk_id="chunk-1",
                logical_chunk_id="logical-chunk-1",
                document_id="document-1",
                document_version_id="version-1",
                record_kind=RecordKind.EVIDENCE,
                chunk_kind=ChunkKind.TEXT,
                connector_type=ConnectorType.LOCAL,
                source_scope_id="scope-1",
                content="The timeout is 30 seconds.",
                citation_locator=CitationLocator(source_element_ids=("paragraph-1",)),
                quality_score=1.0,
            ),
        )
        vector_reference = await repository.put_vector_projection(
            document_id="document-1",
            document_version_id="version-1",
            points=(point,),
            context=context(),
        )
        graph_reference = await repository.put_graph_projection(
            document_id="document-1",
            document_version_id="version-1",
            nodes=(node,),
            relations=(relation,),
            context=context(),
        )

        assert await repository.get_vector_projection(
            vector_reference,
            context=context(),
        ) == (point,)
        assert await repository.get_graph_projection(
            graph_reference,
            context=context(),
        ) == ((node,), (relation,))


@pytest.mark.asyncio
async def test_vector_projection_read_skips_retired_route_records() -> None:
    # Artifacts written before the route collection was retired interleave route points
    # with evidence points. Route points are no longer projected anywhere, so replaying
    # such an artifact must drop them and still yield the evidence — not fail to parse.
    store = MemoryObjectStore()
    writer = ImmutableArtifactWriter(store)
    reader = ImmutableArtifactReader(store)
    repository = ProjectionArtifactRepository(writer, reader)

    def payload(chunk_id: str, record_kind: str) -> dict[str, object]:
        return {
            "chunk_id": chunk_id,
            "logical_chunk_id": f"logical-{chunk_id}",
            "document_id": "document-1",
            "document_version_id": "version-1",
            "record_kind": record_kind,
            "chunk_kind": "text",
            "connector_type": "local",
            "source_scope_id": "scope-1",
            "content": "The timeout is 30 seconds.",
            "citation_locator": {"source_element_ids": ["paragraph-1"]},
            "quality_score": 1.0,
            # Legacy payload keys that the current strict model no longer declares.
            "preview": "The timeout...",
            "content_reference": "s3://legacy/ref",
        }

    def record(point_id: str, chunk_id: str, record_kind: str) -> dict[str, object]:
        return {
            "point_id": point_id,
            "tenant_id": "tenant-1",
            "dense_vector": [0.1, 0.2],
            "sparse_vector": {"indices": [1], "values": [1.0]},
            "payload": payload(chunk_id, record_kind),
        }

    legacy_artifact = "\n".join(
        json.dumps(value)
        for value in (
            record("00000000-0000-5000-8000-000000000001", "chunk-route", "route"),
            record("00000000-0000-5000-8000-000000000002", "chunk-evidence", "evidence"),
        )
    )
    reference = await writer.put(
        ImmutableArtifact(
            bucket=ARTIFACT_BUCKET,
            key=IngestionArtifactLayout.vector_projection("document-1", "version-1"),
            payload=legacy_artifact.encode("utf-8"),
            media_type="application/x-ndjson",
            artifact_kind="vector-projection",
        ),
        context=context(),
    )

    records = await repository.get_vector_projection(reference, context=context())

    assert [record.payload.chunk_id for record in records] == ["chunk-evidence"]
    assert all(record.payload.record_kind is RecordKind.EVIDENCE for record in records)
