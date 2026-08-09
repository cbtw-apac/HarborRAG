from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from harborrag_core.chunking import ChunkKind, ChunkRecord
from harborrag_core.ingestion import (
    ArtifactReference,
    GraphEntityType,
    GraphOwnershipScope,
    GraphProjectionVerification,
    IndexVerificationResult,
    KnowledgeNodeKind,
    ProjectionManifest,
    VectorProjectionVerification,
)
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId

from .graph import GraphProjectionBatch
from .vector import VectorProjectionBatch


@dataclass(frozen=True, slots=True)
class ProjectionManifestInput:
    document_id: str
    document_version_id: str
    chunks: tuple[ChunkRecord, ...]
    vectors: VectorProjectionBatch
    graph: GraphProjectionBatch
    canonical_table_ids: tuple[str, ...] = ()
    table_artifacts: tuple[ArtifactReference, ...] = ()
    canonical_comment_ids: tuple[str, ...] = ()
    comment_artifact: ArtifactReference | None = None
    vector_artifact: ArtifactReference | None = None
    graph_artifact: ArtifactReference | None = None


@dataclass(frozen=True, slots=True)
class ProjectionVerificationInput:
    manifest: ProjectionManifest
    chunks: tuple[ChunkRecord, ...]
    vectors: VectorProjectionBatch
    graph: GraphProjectionBatch
    vector_result: VectorProjectionVerification
    graph_result: GraphProjectionVerification
    canonical_table_ids: tuple[str, ...] | None = None
    canonical_comment_ids: tuple[str, ...] | None = None


class ProjectionManifestBuilder:
    """Describe the exact deterministic records expected in both projections."""

    def build(self, request: ProjectionManifestInput) -> ProjectionManifest:
        return ProjectionManifest(
            document_id=DocumentId(request.document_id),
            document_version_id=DocumentVersionId(request.document_version_id),
            evidence_point_ids=tuple(
                record.point_id for record in request.vectors.evidence_records
            ),
            graph_node_keys=tuple(node.node_key for node in request.graph.nodes),
            graph_relation_ids=tuple(relation.relation_id for relation in request.graph.relations),
            chunk_ids=tuple(str(chunk.chunk_id) for chunk in request.chunks),
            canonical_table_ids=request.canonical_table_ids,
            table_artifacts=request.table_artifacts,
            canonical_comment_ids=request.canonical_comment_ids,
            comment_artifact=request.comment_artifact,
            vector_artifact=request.vector_artifact,
            graph_artifact=request.graph_artifact,
        )


class ProjectionVerifier:
    """Enforce cross-store identity and evidence integrity before publication."""

    def verify(self, request: ProjectionVerificationInput) -> IndexVerificationResult:
        errors: list[str] = []
        chunk_ids = {str(chunk.chunk_id) for chunk in request.chunks}
        if chunk_ids != set(request.manifest.chunk_ids):
            errors.append("chunk manifest does not match the canonical chunk set")
        if set(request.manifest.evidence_point_ids) != {
            record.point_id for record in request.vectors.evidence_records
        }:
            errors.append("evidence point manifest does not match the vector batch")
        if set(request.manifest.graph_node_keys) != {node.node_key for node in request.graph.nodes}:
            errors.append("graph node manifest does not match the graph batch")
        if set(request.manifest.graph_relation_ids) != {
            relation.relation_id for relation in request.graph.relations
        }:
            errors.append("graph relation manifest does not match the graph batch")
        errors.extend(
            self._vector_identity_errors(
                request.manifest,
                request.vectors,
                chunk_ids,
            )
        )
        errors.extend(
            self._graph_identity_errors(
                request.manifest,
                request.graph,
                chunk_ids,
            )
        )
        graph_chunk_ids = {
            node.logical_id
            for node in request.graph.nodes
            if node.node_kind == KnowledgeNodeKind.CHUNK
        }
        vector_chunk_ids = {record.payload.chunk_id for record in request.vectors.evidence_records}
        if graph_chunk_ids != vector_chunk_ids:
            errors.append("graph chunk nodes do not match vector evidence chunks")
        errors.extend(self._table_reference_errors(request.chunks, request.graph))
        if request.canonical_table_ids is not None:
            table_ids = {
                chunk.table_locator.table_id
                for chunk in request.chunks
                if chunk.chunk_kind == ChunkKind.TABLE and chunk.table_locator is not None
            }
            if table_ids != set(request.canonical_table_ids):
                errors.append("table chunk references do not match canonical tables")
            if set(request.manifest.canonical_table_ids) != set(request.canonical_table_ids):
                errors.append("table artifact manifest does not match canonical tables")
        errors.extend(self._comment_reference_errors(request))
        return IndexVerificationResult(
            valid=request.vector_result.valid and request.graph_result.valid and not errors,
            vector=request.vector_result,
            graph=request.graph_result,
            cross_projection_errors=tuple(dict.fromkeys(errors)),
        )

    @staticmethod
    def _vector_identity_errors(
        manifest: ProjectionManifest,
        vectors: VectorProjectionBatch,
        chunk_ids: set[str],
    ) -> list[str]:
        errors: list[str] = []
        for record in vectors.evidence_records:
            payload = record.payload
            if str(payload.document_id) != str(manifest.document_id):
                errors.append("vector payload document ID mismatch")
            if str(payload.document_version_id) != str(manifest.document_version_id):
                errors.append("vector payload document-version ID mismatch")
            if payload.chunk_id not in chunk_ids:
                errors.append("vector payload references an unknown chunk")
        return errors

    @staticmethod
    def _comment_reference_errors(
        request: ProjectionVerificationInput,
    ) -> list[str]:
        canonical_ids = set(request.manifest.canonical_comment_ids)
        if request.canonical_comment_ids is not None and canonical_ids != set(
            request.canonical_comment_ids
        ):
            return ["comment artifact manifest does not match canonical comments"]
        chunk_comment_ids = {
            str(
                chunk.metadata.get("comment_id")
                or next(
                    iter(chunk.citation_locator.source_element_ids),
                    "",
                )
            )
            for chunk in request.chunks
            if chunk.chunk_kind == ChunkKind.COMMENT
        }
        if not chunk_comment_ids <= canonical_ids:
            return ["comment chunks reference comments absent from the canonical comment artifact"]
        return []

    @staticmethod
    def _graph_identity_errors(
        manifest: ProjectionManifest,
        graph: GraphProjectionBatch,
        chunk_ids: set[str],
    ) -> list[str]:
        errors: list[str] = []
        current_nodes = [
            node
            for node in graph.nodes
            if node.ownership_scope == GraphOwnershipScope.DOCUMENT_VERSION
            and str(node.document_id) == str(manifest.document_id)
        ]
        if not current_nodes:
            errors.append("graph batch has no node for the candidate document")
        if any(
            str(node.document_version_id) != str(manifest.document_version_id)
            for node in current_nodes
        ):
            errors.append("graph node document-version ID mismatch")
        for relation in graph.relations:
            if relation.ownership_scope == GraphOwnershipScope.DOCUMENT_VERSION and str(
                relation.document_version_id
            ) != str(manifest.document_version_id):
                errors.append("graph relation document-version ID mismatch")
        graph_chunk_ids = {
            node.logical_id for node in graph.nodes if node.node_kind == KnowledgeNodeKind.CHUNK
        }
        if not graph_chunk_ids <= chunk_ids:
            errors.append("graph contains an unknown chunk node")
        return errors

    @staticmethod
    def _table_reference_errors(
        chunks: Sequence[ChunkRecord],
        graph: GraphProjectionBatch,
    ) -> list[str]:
        chunk_table_ids = {
            chunk.table_locator.table_id
            for chunk in chunks
            if chunk.chunk_kind == ChunkKind.TABLE and chunk.table_locator is not None
        }
        graph_table_ids = {
            node.logical_id
            for node in graph.nodes
            if node.entity_type == GraphEntityType.TABLE
            and str(node.document_id) == str(chunks[0].document_id)
        }
        if chunk_table_ids != graph_table_ids:
            return ["table chunk references do not match graph table nodes"]
        return []
