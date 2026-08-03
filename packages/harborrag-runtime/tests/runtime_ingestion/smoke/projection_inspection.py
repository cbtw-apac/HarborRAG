from __future__ import annotations

import asyncio
from dataclasses import dataclass

from harborrag_adapters.repositories.graph.falkordb import (
    FalkorKnowledgeGraphRepository,
)
from harborrag_adapters.repositories.object_store import (
    ChunkArtifactReader,
    ImmutableArtifactReader,
)
from harborrag_adapters.repositories.object_store.s3 import S3ObjectStore
from harborrag_adapters.repositories.vector import HarborVectorRepository
from harborrag_core.ingestion import (
    ContentReference,
    DocumentIdentityBuilder,
    KnowledgeNodeKind,
    reject_runtime_fields,
)
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_core.schemas.vector import VectorIndexRecord
from harborrag_engine.ingestion import EVIDENCE_INDEX, ROUTE_INDEX

_REQUIRED_VECTOR_PAYLOAD_FIELDS = frozenset(
    {
        "chunk_id",
        "logical_chunk_id",
        "document_id",
        "document_version_id",
        "record_kind",
        "chunk_kind",
        "connector_type",
        "document_kind",
        "source_scope_id",
        "source_item_id",
        "content_reference",
        "content",
        "preview",
        "section_path",
        "token_count",
        "content_hash",
        "citation_locator",
        "quality_score",
    }
)
_ALLOWED_VECTOR_PAYLOAD_FIELDS = _REQUIRED_VECTOR_PAYLOAD_FIELDS | frozenset(
    {
        "language",
        "document_title",
        "relative_path",
        "space_id",
        "page_id",
        "project_id",
        "issue_key",
        "attachment_id",
    }
)


@dataclass(frozen=True, slots=True)
class ChunkObservation:
    collection: str
    chunk_id: str
    document_id: str
    chunk_kind: str
    dense_dimensions: int
    sparse_terms: int
    preview: str
    content: str
    payload_fields: tuple[str, ...]
    citation_fields: tuple[str, ...]
    content_byte_offset: int
    content_byte_length: int


@dataclass(frozen=True, slots=True)
class GraphObservation:
    document_id: str
    nodes: int
    relations: int
    duplicate_relations: int
    relation_types: tuple[str, ...]
    outgoing_relations: int
    incoming_relations: int
    reviewable_nodes: int
    sourced_nodes: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class ProjectionStores:
    vectors: HarborVectorRepository
    graph: FalkorKnowledgeGraphRepository
    objects: S3ObjectStore


async def inspect_projections(
    *,
    stores: ProjectionStores,
    documents: tuple[tuple[str, str], ...],
    versions: frozenset[str],
    context: StorageOperationContext,
) -> tuple[tuple[ChunkObservation, ...], tuple[GraphObservation, ...]]:
    points = await _projection_points(
        stores.vectors,
        versions=versions,
        context=context,
    )
    chunks = await _chunk_observations(
        points,
        reader=ChunkArtifactReader(ImmutableArtifactReader(stores.objects)),
        context=context,
    )
    graphs = await _graph_observations(
        stores.graph,
        documents=documents,
        context=context,
    )
    _assert_projection_content(chunks, graphs)
    return chunks, graphs


async def _projection_points(
    repository,
    *,
    versions: frozenset[str],
    context: StorageOperationContext,
) -> tuple[tuple[str, VectorIndexRecord], ...]:
    found: list[tuple[str, VectorIndexRecord]] = []
    for collection in (ROUTE_INDEX, EVIDENCE_INDEX):
        cursor = None
        while True:
            page = await repository.scan_records(
                collection,
                limit=100,
                cursor=cursor,
                context=context,
            )
            found.extend(
                (collection, point)
                for point in page.records
                if point.payload.get("document_version_id") in versions
            )
            cursor = page.next_cursor
            if cursor is None:
                break
    if not found:
        raise AssertionError("no Qdrant points resolved for active versions")
    return tuple(found)


async def _chunk_observations(
    points: tuple[tuple[str, VectorIndexRecord], ...],
    *,
    reader: ChunkArtifactReader,
    context: StorageOperationContext,
) -> tuple[ChunkObservation, ...]:
    observations: list[ChunkObservation] = []
    for collection, point in points:
        reference = validate_vector_payload(collection, point)
        sparse = point.sparse_vector
        assert sparse is not None
        assert reference.byte_offset is not None
        assert reference.byte_length is not None
        chunk = await reader.get_reference(reference, context=context)
        if str(chunk.chunk_id) != point.payload.get("chunk_id"):
            raise AssertionError("Qdrant byte range resolved to a different chunk")
        if point.payload.get("content") != chunk.content:
            raise AssertionError("Qdrant review content differs from the canonical chunk")
        citation = point.payload["citation_locator"]
        assert isinstance(citation, dict)
        observations.append(
            ChunkObservation(
                collection=collection,
                chunk_id=str(chunk.chunk_id),
                document_id=str(chunk.document_id),
                chunk_kind=chunk.chunk_kind.value,
                dense_dimensions=len(point.vector),
                sparse_terms=len(sparse.indices),
                preview=str(point.payload.get("preview") or "")[:160],
                content=chunk.content,
                payload_fields=tuple(sorted(point.payload)),
                citation_fields=tuple(sorted(citation)),
                content_byte_offset=reference.byte_offset,
                content_byte_length=reference.byte_length,
            )
        )
    return tuple(sorted(observations, key=lambda item: (item.collection, item.chunk_id)))


async def _graph_observations(
    repository,
    *,
    documents: tuple[tuple[str, str], ...],
    context: StorageOperationContext,
) -> tuple[GraphObservation, ...]:
    identities = DocumentIdentityBuilder()
    observations = []
    for document_id, document_version_id in documents:
        node_key = identities.node_key(
            node_kind=KnowledgeNodeKind.DOCUMENT,
            logical_id=document_id,
            document_version_id=document_version_id,
        )
        traversal, outgoing, incoming = await asyncio.gather(
            *(
                repository.traverse(
                    node_key,
                    max_depth=3,
                    max_nodes=200,
                    direction=direction,
                    context=context,
                )
                for direction in ("both", "outgoing", "incoming")
            )
        )
        relation_keys = tuple(
            (
                relation.relation_type.value,
                relation.source_node_key,
                relation.target_node_key,
            )
            for relation in traversal.relations
        )
        observations.append(
            GraphObservation(
                document_id=document_id,
                nodes=len(traversal.nodes),
                relations=len(traversal.relations),
                duplicate_relations=len(relation_keys) - len(set(relation_keys)),
                relation_types=tuple(
                    sorted({relation.relation_type.value for relation in traversal.relations})
                ),
                outgoing_relations=len(outgoing.relations),
                incoming_relations=len(incoming.relations),
                reviewable_nodes=sum(
                    bool(node.title or node.content_preview) for node in traversal.nodes
                ),
                sourced_nodes=sum(bool(node.source_item_id) for node in traversal.nodes),
                truncated=(traversal.truncated or outgoing.truncated or incoming.truncated),
            )
        )
    return tuple(observations)


def validate_vector_payload(
    collection: str,
    point: VectorIndexRecord,
) -> ContentReference:
    """Assert the retrieval payload is complete, bounded, and runtime-free."""

    reject_runtime_fields(point.payload)
    _assert_payload_shape(collection, point)
    _assert_vector_shape(point)
    reference = ContentReference.model_validate(point.payload["content_reference"])
    if not reference.byte_length:
        raise AssertionError("Qdrant content reference must identify one byte range")
    return reference


def _assert_payload_shape(collection: str, point: VectorIndexRecord) -> None:
    missing = _REQUIRED_VECTOR_PAYLOAD_FIELDS.difference(point.payload)
    if missing:
        raise AssertionError(f"Qdrant payload is missing fields: {sorted(missing)}")
    unknown = set(point.payload).difference(_ALLOWED_VECTOR_PAYLOAD_FIELDS)
    if unknown:
        raise AssertionError(f"Qdrant payload has unknown fields: {sorted(unknown)}")
    if any(value is None for value in point.payload.values()):
        raise AssertionError("Qdrant payload must omit irrelevant null fields")
    expected_kind = "route" if collection == ROUTE_INDEX else "evidence"
    if point.payload["record_kind"] != expected_kind:
        raise AssertionError("Qdrant record kind does not match its collection")


def _assert_vector_shape(point: VectorIndexRecord) -> None:
    if not point.vector:
        raise AssertionError("Qdrant point is missing its dense vector")
    if point.sparse_vector is None or not point.sparse_vector.indices:
        raise AssertionError("Qdrant point is missing its sparse vector")
    if len(str(point.payload["preview"])) > 320:
        raise AssertionError("Qdrant preview exceeds its payload limit")
    if not isinstance(point.payload["citation_locator"], dict):
        raise AssertionError("Qdrant citation locator must be structured")


def _assert_projection_content(
    chunks: tuple[ChunkObservation, ...],
    graphs: tuple[GraphObservation, ...],
) -> None:
    if not any(chunk.collection == ROUTE_INDEX for chunk in chunks):
        raise AssertionError("Qdrant route collection has no smoke chunk")
    if not any(chunk.collection == EVIDENCE_INDEX for chunk in chunks):
        raise AssertionError("Qdrant evidence collection has no smoke chunk")
    if not any(chunk.chunk_kind == "table" for chunk in chunks):
        raise AssertionError("Qdrant evidence collection has no table chunk")
    content = "\n".join(chunk.content for chunk in chunks)
    for expected in (
        "ingestion activity timeout is exactly 30 seconds",
        "HARBOR-4242",
        "Postgres remains the publication authority",
    ):
        if expected.casefold() not in content.casefold():
            raise AssertionError(f"range-loaded chunk content is missing: {expected}")
    _assert_graph_content(graphs)


def _assert_graph_content(graphs: tuple[GraphObservation, ...]) -> None:
    if any(graph.truncated for graph in graphs):
        raise AssertionError("smoke graph traversal was truncated")
    if any(graph.duplicate_relations for graph in graphs):
        raise AssertionError("smoke graph traversal contains duplicate semantic relations")
    if any(not graph.outgoing_relations or not graph.incoming_relations for graph in graphs):
        raise AssertionError("smoke graph must support outgoing and reverse traversal")
    if any(not graph.reviewable_nodes or not graph.sourced_nodes for graph in graphs):
        raise AssertionError("smoke graph nodes must expose reviewable source context")
    relation_types = {relation for graph in graphs for relation in graph.relation_types}
    if "links_to" not in relation_types:
        raise AssertionError("local document LINKS_TO relation is missing")
    if "has_section" not in relation_types:
        raise AssertionError("document section structure is missing")
    if "has_table" not in relation_types:
        raise AssertionError("document table structure is missing")
