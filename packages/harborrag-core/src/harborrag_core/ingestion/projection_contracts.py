from __future__ import annotations

from pydantic import Field, model_validator

from harborrag_core.base import StrictModel
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId

from .artifact_contracts import ArtifactReference
from .projection_graph import (
    GRAPH_SCHEMA_VERSION,
    GraphEdgeRecord,
    GraphNodeRecord,
    GraphProjectionManifest,
)
from .projection_vector import (
    VectorEvidenceRecord,
    VectorPayload,
    VectorProjectionBatch,
    VectorProjectionManifest,
)

__all__ = [
    "GRAPH_SCHEMA_VERSION",
    "GraphEdgeRecord",
    "GraphNodeRecord",
    "GraphProjectionManifest",
    "VectorEvidenceRecord",
    "VectorPayload",
    "VectorProjectionBatch",
    "VectorProjectionManifest",
]


class ProjectionManifest(StrictModel):
    """Expected cross-store projection contents for one document version."""

    document_id: DocumentId
    document_version_id: DocumentVersionId
    # Retired: the separate route collection is gone and nothing writes this. It stays
    # because the manifest is persisted and StrictModel forbids unknown fields, so every
    # manifest row written before the retirement carries the key. Most of those rows
    # belong to versions that are still active and will therefore never be cleaned up,
    # so the field cannot be retired by waiting for a drain. Always empty on new writes.
    route_point_ids: tuple[str, ...] = ()
    evidence_point_ids: tuple[str, ...] = ()
    graph_node_keys: tuple[str, ...] = ()
    graph_relation_ids: tuple[str, ...] = ()
    chunk_ids: tuple[str, ...] = ()
    canonical_table_ids: tuple[str, ...] = ()
    table_artifacts: tuple[ArtifactReference, ...] = ()
    canonical_comment_ids: tuple[str, ...] = ()
    comment_artifact: ArtifactReference | None = None
    vector_artifact: ArtifactReference | None = None
    graph_artifact: ArtifactReference | None = None

    @model_validator(mode="after")
    def validate_unique_identities(self) -> ProjectionManifest:
        groups = (
            self.route_point_ids,
            self.evidence_point_ids,
            self.graph_node_keys,
            self.graph_relation_ids,
            self.chunk_ids,
            self.canonical_table_ids,
            self.canonical_comment_ids,
        )
        if any(len(set(group)) != len(group) for group in groups):
            raise ValueError("projection manifest identities must be unique")
        if len(self.canonical_table_ids) != len(self.table_artifacts):
            raise ValueError("canonical table IDs and table artifact references must align")
        for table_id, reference in zip(
            self.canonical_table_ids,
            self.table_artifacts,
            strict=True,
        ):
            if not reference.key.endswith(f"/{table_id}.parquet"):
                raise ValueError("canonical table artifact key does not match its table ID")
        if self.canonical_comment_ids and self.comment_artifact is None:
            raise ValueError("canonical comments require an immutable comment artifact")
        return self


class GraphProjectionVerification(StrictModel):
    """Read-after-write verification for one staged graph projection."""

    valid: bool
    expected_node_count: int = Field(ge=0)
    actual_node_count: int = Field(ge=0)
    expected_relation_count: int = Field(ge=0)
    actual_relation_count: int = Field(ge=0)
    missing_node_keys: tuple[str, ...] = ()
    missing_relation_ids: tuple[str, ...] = ()
    dangling_relation_ids: tuple[str, ...] = ()
    duplicate_node_keys: tuple[str, ...] = ()
    duplicate_relation_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_outcome(self) -> GraphProjectionVerification:
        has_issues = any(
            (
                self.missing_node_keys,
                self.missing_relation_ids,
                self.dangling_relation_ids,
                self.duplicate_node_keys,
                self.duplicate_relation_ids,
            )
        )
        counts_match = (
            self.expected_node_count == self.actual_node_count
            and self.expected_relation_count == self.actual_relation_count
        )
        if self.valid != (counts_match and not has_issues):
            raise ValueError("graph verification validity does not match its findings")
        return self


class GraphSchemaMigrationVerification(StrictModel):
    """Tenant-level gate required before schema-v1 graph records are removed."""

    valid: bool
    missing_chunk_ids: tuple[str, ...] = ()
    invalid_source_item_ids: tuple[str, ...] = ()
    content_field_records: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_outcome(self) -> GraphSchemaMigrationVerification:
        expected = not (
            self.missing_chunk_ids or self.invalid_source_item_ids or self.content_field_records
        )
        if self.valid != expected:
            raise ValueError("graph migration verification validity does not match findings")
        return self


class VectorProjectionVerification(StrictModel):
    """Read-after-write verification for the Qdrant evidence collection."""

    valid: bool
    expected_evidence_count: int = Field(ge=0)
    actual_evidence_count: int = Field(ge=0)
    missing_point_ids: tuple[str, ...] = ()
    invalid_dense_point_ids: tuple[str, ...] = ()
    invalid_sparse_point_ids: tuple[str, ...] = ()
    mismatched_payload_point_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_outcome(self) -> VectorProjectionVerification:
        has_issues = any(
            (
                self.missing_point_ids,
                self.invalid_dense_point_ids,
                self.invalid_sparse_point_ids,
                self.mismatched_payload_point_ids,
            )
        )
        counts_match = self.expected_evidence_count == self.actual_evidence_count
        if self.valid != (counts_match and not has_issues):
            raise ValueError("vector verification validity does not match its findings")
        return self


class IndexVerificationResult(StrictModel):
    """Combined pre-publication verification across both projections."""

    valid: bool
    vector: VectorProjectionVerification
    graph: GraphProjectionVerification
    cross_projection_errors: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_outcome(self) -> IndexVerificationResult:
        expected = self.vector.valid and self.graph.valid and not self.cross_projection_errors
        if self.valid != expected:
            raise ValueError("index verification validity does not match its findings")
        return self


class KnowledgeGraphTraversal(StrictModel):
    """Bounded graph traversal returned without provider-internal node IDs."""

    nodes: tuple[GraphNodeRecord, ...]
    relations: tuple[GraphEdgeRecord, ...]
    truncated: bool = False
