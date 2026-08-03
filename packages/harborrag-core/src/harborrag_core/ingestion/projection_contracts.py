from __future__ import annotations

from pydantic import Field, field_validator, model_validator

from harborrag_core.base import StrictModel
from harborrag_core.chunking import ConnectorType, DocumentKind, RelationType
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId

from .artifact_contracts import ArtifactReference
from .projection_vector import (
    VectorEvidenceRecord,
    VectorPayload,
    VectorProjectionBatch,
    VectorProjectionManifest,
    VectorRouteRecord,
)
from .states import KnowledgeNodeKind

__all__ = [
    "VectorEvidenceRecord",
    "VectorPayload",
    "VectorProjectionBatch",
    "VectorProjectionManifest",
    "VectorRouteRecord",
]


class GraphEdgeRecord(StrictModel):
    """One deterministic structural or source-explicit graph relation."""

    relation_id: str = Field(min_length=1)
    relation_type: RelationType
    source_node_key: str = Field(min_length=1)
    target_node_key: str = Field(min_length=1)
    document_version_id: DocumentVersionId
    source_relation_version: str = Field(min_length=1)
    source_explicit: bool
    evidence_chunk_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_relation(self) -> GraphEdgeRecord:
        if self.source_node_key == self.target_node_key:
            raise ValueError("canonical relations must not self-reference")
        if len(set(self.evidence_chunk_ids)) != len(self.evidence_chunk_ids):
            raise ValueError("evidence chunk IDs must be unique")
        return self


class GraphProjectionManifest(StrictModel):
    """Deterministic identity and checksum for one structural graph batch."""

    schema_version: str = "1.0"
    document_id: DocumentId
    document_version_id: DocumentVersionId
    node_keys: tuple[str, ...] = Field(min_length=1)
    relation_ids: tuple[str, ...]
    payload_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_identities(self) -> GraphProjectionManifest:
        if len(set(self.node_keys)) != len(self.node_keys):
            raise ValueError("graph manifest node keys must be unique")
        if len(set(self.relation_ids)) != len(self.relation_ids):
            raise ValueError("graph manifest relation IDs must be unique")
        return self


class GraphNodeRecord(StrictModel):
    """Projection-neutral graph node with bounded operator-facing context."""

    node_key: str = Field(min_length=1)
    node_kind: KnowledgeNodeKind
    logical_id: str = Field(min_length=1)
    document_id: DocumentId
    document_version_id: DocumentVersionId
    source_scope_id: str = Field(min_length=1)
    title: str | None = None
    connector_type: ConnectorType | None = None
    document_kind: DocumentKind | None = None
    source_item_id: str | None = None
    source_uri: str | None = None
    content_preview: str | None = Field(default=None, max_length=1_000)
    section_path: tuple[str, ...] = ()

    @field_validator("title", "source_item_id", "source_uri", "content_preview")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("graph node text must be non-empty when supplied")
        return value

    @model_validator(mode="after")
    def validate_review_context(self) -> GraphNodeRecord:
        if any(not value.strip() for value in self.section_path):
            raise ValueError("graph node section path entries must be non-empty")
        return self


class ProjectionManifest(StrictModel):
    """Expected cross-store projection contents for one document version."""

    document_id: DocumentId
    document_version_id: DocumentVersionId
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
    missing_evidence_chunk_ids: tuple[str, ...] = ()
    duplicate_node_keys: tuple[str, ...] = ()
    duplicate_relation_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_outcome(self) -> GraphProjectionVerification:
        has_issues = any(
            (
                self.missing_node_keys,
                self.missing_relation_ids,
                self.dangling_relation_ids,
                self.missing_evidence_chunk_ids,
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


class VectorProjectionVerification(StrictModel):
    """Read-after-write verification for both Qdrant projection collections."""

    valid: bool
    expected_route_count: int = Field(ge=0)
    actual_route_count: int = Field(ge=0)
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
        counts_match = (
            self.expected_route_count == self.actual_route_count
            and self.expected_evidence_count == self.actual_evidence_count
        )
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
