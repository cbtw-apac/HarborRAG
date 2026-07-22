from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from harborrag_core.models.embed import (
    HarborEmbedding,
    HarborEmbedRequest,
    HarborEmbedResponse,
)
from harborrag_core.schemas.documents import ChunkContext, ChunkRecord, ChunkSourceSpan
from harborrag_core.schemas.graph import GraphEdge, GraphNode
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_core.schemas.vector import VectorCollectionSpec, VectorPoint
from harborrag_engine.ingestion.chunking.schemas import (
    ChunkingDiagnostics,
    ChunkingResult,
    ChunkManifest,
    ChunkReference,
    ChunkValidationResult,
)
from harborrag_engine.ingestion.indexing import IndexingConfig, IndexingRequest


class CharacterCounter:
    def count(self, text: str) -> int:
        return len(text)


def make_reference(
    logical_id: str,
    revision_id: str,
    content_hash: str,
    *,
    ordinal: int,
    token_count: int = 4,
    body_uri: str | None = None,
) -> ChunkReference:
    return ChunkReference(
        logical_chunk_id=logical_id,
        chunk_revision_id=revision_id,
        ordinal=ordinal,
        content_hash=content_hash,
        token_count=token_count,
        body_uri=body_uri,
    )


def make_manifest(
    references: Sequence[ChunkReference],
    *,
    artifact_revision_id: str,
    artifact_id: str = "artifact-1",
    tenant_id: str = "tenant-1",
) -> ChunkManifest:
    chunks = tuple(references)
    return ChunkManifest(
        tenant_id=tenant_id,
        artifact_id=artifact_id,
        artifact_revision_id=artifact_revision_id,
        chunker_name="document",
        chunker_version="1",
        configuration_hash="chunk-config-1",
        chunks=chunks,
        total_token_count=sum(reference.token_count for reference in chunks),
        total_chunk_count=len(chunks),
        validation=ChunkValidationResult(valid=True),
        fingerprint=f"manifest-{artifact_revision_id}",
    )


def make_record(
    reference: ChunkReference,
    *,
    artifact_revision_id: str,
    artifact_id: str = "artifact-1",
    tenant_id: str = "tenant-1",
    content: str | None = None,
    role: str = "content",
    structural_path: tuple[str, ...] = ("Guide", "Setup"),
    metadata: dict[str, Any] | None = None,
    page_start: int | None = 1,
    page_end: int | None = 1,
) -> ChunkRecord:
    text = content or f"text for {reference.logical_chunk_id}"
    span = ChunkSourceSpan(page_start=page_start, page_end=page_end)
    context = ChunkContext(title="HarborRAG", structural_path=structural_path)
    return ChunkRecord(
        id=reference.chunk_revision_id,
        logical_chunk_id=reference.logical_chunk_id,
        chunk_revision_id=reference.chunk_revision_id,
        tenant_id=tenant_id,
        document_id=artifact_id,
        document_version_id=artifact_revision_id,
        artifact_id=artifact_id,
        artifact_revision_id=artifact_revision_id,
        chunk_index=reference.ordinal,
        ordinal=reference.ordinal,
        role=role,
        content=text,
        content_hash=reference.content_hash,
        token_count=reference.token_count,
        source_span=span,
        context=context,
        structural_path=structural_path,
        page_start=page_start,
        page_end=page_end,
        metadata={"source_kind": "document", **(metadata or {})},
    )


def make_chunking_result(
    manifest: ChunkManifest,
    records: Sequence[ChunkRecord],
) -> ChunkingResult:
    values = tuple(records)
    return ChunkingResult(
        artifact_id=manifest.artifact_id,
        artifact_revision_id=manifest.artifact_revision_id,
        strategy=manifest.chunker_name,
        profile="document",
        profile_hash=manifest.configuration_hash,
        chunks=values,
        diagnostics=ChunkingDiagnostics(
            strategy=manifest.chunker_name,
            profile="document",
            source_units=len(values),
            oversized_units=0,
            forced_splits=0,
            merged_units=0,
            final_chunks=len(values),
            total_tokens=sum(record.token_count or 0 for record in values),
        ),
        manifest=manifest,
    )


def make_config(**changes: Any) -> IndexingConfig:
    values: dict[str, Any] = {
        "embedding_model": "documents",
        "embedding_dimensions": 3,
        "vector_collection": "documents",
        "graph_namespace": "knowledge",
        "embedding_context_maximum_characters": 128,
        "capsule_maximum_characters": 32,
    }
    values.update(changes)
    return IndexingConfig(**values)


def make_index_request(
    *,
    proposed: ChunkManifest,
    records: Sequence[ChunkRecord],
    active: ChunkManifest | None = None,
    active_fingerprint: str | None = None,
    active_generation_id: str | None = None,
    config: IndexingConfig | None = None,
) -> IndexingRequest:
    return IndexingRequest(
        chunking=make_chunking_result(proposed, records),
        generation_id="generation-2",
        config=config or make_config(),
        active_manifest=active,
        active_embedding_configuration_fingerprint=active_fingerprint,
        active_generation_id=active_generation_id,
        context=StorageOperationContext(tenant_id="tenant-1"),
    )


class FakeEmbedClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.requests: list[HarborEmbedRequest] = []

    async def aembed(
        self,
        inputs: Any = None,
        *,
        request: HarborEmbedRequest | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> HarborEmbedResponse:
        del inputs, model, kwargs
        if self.fail:
            raise RuntimeError("embedding failed")
        assert request is not None
        self.requests.append(request)
        dimensions = request.dimensions or 3
        embeddings = tuple(
            HarborEmbedding(
                index=index,
                value=tuple(float(index + offset + 1) for offset in range(dimensions)),
                dimensions=dimensions,
            )
            for index in range(len(request.inputs))
        )
        return HarborEmbedResponse(
            embeddings=embeddings,
            logical_model=request.logical_model or "documents",
            embedding_space="documents-v1",
            provider="fake",
            provider_model="fake-embed",
            deployment="test",
            request_id=f"request-{len(self.requests)}",
        )

    async def aclose(self) -> None:
        return None


class FakeVectorRepository:
    def __init__(self, *, fail: bool = False, activate_on_read: bool = False) -> None:
        self.fail = fail
        self.activate_on_read = activate_on_read
        self.specs: list[VectorCollectionSpec] = []
        self.upsert_calls = 0
        self.points: dict[str, VectorPoint] = {}

    async def ensure_collection(self, spec, *, context) -> None:
        del context
        self.specs.append(spec)

    async def upsert(self, collection, points, *, context) -> None:
        del collection, context
        if self.fail:
            raise RuntimeError("vector write failed")
        self.upsert_calls += 1
        self.points.update({point.id: point for point in points})

    async def get(self, collection, ids, *, context) -> list[VectorPoint]:
        del collection, context
        points = [self.points[point_id] for point_id in ids if point_id in self.points]
        if self.activate_on_read:
            return [
                point.model_copy(update={"payload": {**point.payload, "is_active": True}})
                for point in points
            ]
        return points


class FakeGraphRepository:
    def __init__(self, *, fail: bool = False, drop_edge: bool = False) -> None:
        self.fail = fail
        self.drop_edge = drop_edge
        self.node_upserts = 0
        self.edge_upserts = 0
        self.nodes: dict[str, GraphNode] = {}
        self.edges: dict[str, GraphEdge] = {}

    async def upsert_nodes(self, nodes, *, context) -> None:
        del context
        if self.fail:
            raise RuntimeError("graph write failed")
        self.node_upserts += 1
        self.nodes.update({str(node.id): node for node in nodes})

    async def upsert_edges(self, edges, *, context) -> None:
        del context
        self.edge_upserts += 1
        self.edges.update({str(edge.id): edge for edge in edges})

    async def get_nodes(self, ids, *, context) -> list[GraphNode]:
        del context
        return [self.nodes[str(node_id)] for node_id in ids if str(node_id) in self.nodes]

    async def get_edges(self, ids, *, context) -> list[GraphEdge]:
        del context
        values = [self.edges[str(edge_id)] for edge_id in ids if str(edge_id) in self.edges]
        return values[1:] if self.drop_edge and values else values
