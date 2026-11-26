"""Evidence points built from canonical chunks for the vector index.

Building a projection never writes anywhere. The same chunks always give the
same points and payloads.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from harborrag_core.chunking import ChunkRecord, RecordKind, RelationType
from harborrag_core.ingestion import (
    ChunkSetArtifacts,
    DocumentIdentityBuilder,
    KnowledgeNodeKind,
    RepresentationSet,
    VectorEvidenceRecord,
    VectorPayload,
    VectorProjectionBatch,
    reject_runtime_fields,
)

from ..graph.graph_models import GraphProjectionBatch

EVIDENCE_INDEX = "evidence"
_TIME_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("created_at", "created_at", "source_created_at"),
    ("last_modified", "updated_at", "source_updated_at"),
)
"""Payload key, chunk metadata key and document fallback key of each time field."""


@dataclass(frozen=True, slots=True)
class VectorProjectionInput:
    """One document version's chunks and vectors, ready for projection.

    Attributes:
        chunks: The version's canonical chunks, in order.
        representations: Dense and sparse vectors for those chunks.
        chunk_artifacts: Where the chunks are stored.
        graph: The same version's graph batch, when built. It supplies each
            point's graph keys.
    """

    chunks: tuple[ChunkRecord, ...]
    representations: RepresentationSet
    chunk_artifacts: ChunkSetArtifacts
    graph: GraphProjectionBatch | None = None


class VectorProjectionBuilder:
    """Builder of evidence points from canonical chunks, with no external writes."""

    def __init__(self) -> None:
        self._identity = DocumentIdentityBuilder()

    def build(self, request: VectorProjectionInput) -> VectorProjectionBatch:
        """Build the evidence points of one document version.

        Route records are skipped.

        Args:
            request: The version's chunks, their vectors and chunk artifacts.

        Returns:
            One evidence point per evidence chunk, with the batch manifest.
        """
        if not request.chunks:
            raise ValueError("vector projection requires canonical chunks")
        representations = {record.chunk_id: record for record in request.representations.records}
        first = request.chunks[0]
        if str(request.representations.document_id) != str(first.document_id) or str(
            request.representations.document_version_id
        ) != str(first.document_version_id):
            raise ValueError("representation set belongs to another document version")
        chunk_nodes, source_node = _graph_keys(request.graph)
        evidence_records: list[VectorEvidenceRecord] = []
        for chunk in request.chunks:
            if chunk.record_kind != RecordKind.EVIDENCE:
                continue
            chunk_id = str(chunk.chunk_id)
            representation = representations.get(chunk_id)
            if representation is None:
                raise ValueError(f"representation is missing for chunk {chunk_id}")
            payload = self._payload(
                chunk,
                graph_chunk_node_key=chunk_nodes.get(chunk_id),
                graph_source_node_key=source_node,
            )
            record = VectorEvidenceRecord(
                point_id=self._identity.point_id(chunk_id=chunk_id),
                tenant_id=chunk.tenant_id,
                dense_vector=tuple(representation.dense_vector),
                sparse_vector=representation.sparse_vector,
                payload=payload,
            )
            evidence_records.append(record)
        return VectorProjectionBatch.assemble(evidence_records=tuple(evidence_records))

    def _payload(
        self,
        chunk: ChunkRecord,
        *,
        graph_chunk_node_key: str | None = None,
        graph_source_node_key: str | None = None,
    ) -> VectorPayload:
        """Build one evidence chunk's payload.

        A chunk's own ``created_at`` and ``updated_at`` win, because a comment or
        an attachment is written and edited on its own clock. The document's
        times fill in otherwise, so every point can be narrowed or sorted by
        time. The update time is stored as ``last_modified``. ``item_type``
        holds Jira's ``issue_type`` or another source's ``item_type``, one name
        so a filter by kind need not branch per connector. Blank text is left
        out.
        """
        metadata = chunk.metadata
        payload: dict[str, object] = {
            "chunk_id": str(chunk.chunk_id),
            "logical_chunk_id": str(chunk.logical_chunk_id),
            "document_id": str(chunk.document_id),
            "document_version_id": str(chunk.document_version_id),
            "record_kind": chunk.record_kind.value,
            "chunk_kind": chunk.chunk_kind.value,
            "connector_type": chunk.connector_type.value,
            "document_kind": chunk.document_kind.value,
            "source_scope_id": chunk.source_scope_id,
            "source_item_id": chunk.source_item_id,
            "language": chunk.language,
            "content": chunk.content,
            "document_title": chunk.hierarchy.document_title,
            "section_path": chunk.hierarchy.section_path,
            "token_count": chunk.token_count,
            "content_hash": chunk.content_hash,
            "citation_locator": chunk.citation_locator,
            "quality_score": chunk.quality.score,
        }
        for field in (
            "relative_path",
            "space_id",
            "page_id",
            "project_id",
            "issue_key",
            "attachment_id",
            "site_id",
            "drive_id",
            "item_id",
            "web_url",
            "status",
            "status_category",
            "priority",
            "author",
            "assignee",
            "reporter",
            "creator",
            "project_key",
            "project_name",
            "resolved_at",
            "due_date",
        ):
            text = _payload_text(metadata.get(field))
            if text is not None:
                payload[field] = text
        for payload_field, metadata_field, fallback in _TIME_FIELDS:
            time = _payload_text(metadata.get(metadata_field)) or _payload_text(
                metadata.get(fallback)
            )
            if time is not None:
                payload[payload_field] = time
        parent = metadata.get("parent_source_item_id")
        if isinstance(parent, str) and parent.strip():
            payload["parent_source_item_id"] = parent
        item_type = _payload_text(metadata.get("issue_type")) or _payload_text(
            metadata.get("item_type")
        )
        if item_type is not None:
            payload["item_type"] = item_type
        for field in ("labels", "components"):
            payload[field] = self._text_sequence(metadata.get(field))
        fields = metadata.get("fields")
        if isinstance(fields, Mapping) and fields:
            payload["fields"] = dict(fields)
        payload.update(
            (key, value)
            for key, value in (
                ("graph_chunk_node_key", graph_chunk_node_key),
                ("graph_source_node_key", graph_source_node_key),
            )
            if value
        )
        reject_runtime_fields(payload)
        return VectorPayload.model_validate(payload)

    @staticmethod
    def _text_sequence(value: object) -> tuple[str, ...]:
        """Return the non-blank entries of a list or tuple as stripped text.

        Any other value, a string included, gives an empty tuple.
        """
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            return ()
        return tuple(text for item in value if item is not None and (text := str(item).strip()))


def _payload_text(value: object) -> str | None:
    """Return a string or integer as payload text, unstripped.

    Any other type, and blank text, gives ``None``. The payload model rejects
    blank optional text, so a blank value is left out instead of failing the
    whole projection batch.
    """
    if not isinstance(value, (str, int)):
        return None
    text = str(value)
    return text if text.strip() else None


def _graph_keys(graph: GraphProjectionBatch | None) -> tuple[dict[str, str], str | None]:
    """Return each chunk's Chunk node key and the version's source entity node key."""
    if graph is None:
        return {}, None
    chunk_nodes = {
        node.logical_id: node.node_key
        for node in graph.nodes
        if node.node_kind == KnowledgeNodeKind.CHUNK
    }
    source_node = next(
        (
            relation.source_node_key
            for relation in graph.relations
            if relation.relation_type is RelationType.HAS_VERSION
        ),
        None,
    )
    return chunk_nodes, source_node
