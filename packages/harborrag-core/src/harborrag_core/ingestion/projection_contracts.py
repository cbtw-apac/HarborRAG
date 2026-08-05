from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from harborrag_core.base import StrictModel
from harborrag_core.chunking import RelationType
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId, TenantId

from .artifact_contracts import ArtifactReference
from .identity import reject_runtime_fields
from .projection_vector import (
    VectorEvidenceRecord,
    VectorPayload,
    VectorProjectionBatch,
    VectorProjectionManifest,
)
from .states import GraphEntityType, GraphOwnershipScope, KnowledgeNodeKind

GRAPH_SCHEMA_VERSION: Literal["2.0"] = "2.0"
_MAX_GRAPH_ATTRIBUTES = 32
_MAX_ATTRIBUTE_KEY_LENGTH = 64
_MAX_ATTRIBUTE_TEXT_LENGTH = 1_024
_MAX_ATTRIBUTE_SEQUENCE = 32
_FORBIDDEN_ATTRIBUTE_TOKENS = frozenset(
    {
        "access_token",
        "api_key",
        "body",
        "content",
        "credential",
        "credentials",
        "password",
        "payload",
        "preview",
        "raw",
        "secret",
        "text",
        "token",
    }
)
_ALLOWED_ATTRIBUTE_FIELDS = frozenset(
    {
        "connector_type",
        "connection_id",
        "ctag",
        "default_branch",
        "display_name",
        "document_kind",
        "drive_type",
        "etag",
        "issue_type",
        "issue_key",
        "item_name",
        "mode",
        "name",
        "ordinal",
        "page_id",
        "parent_relative_path",
        "placeholder",
        "project_key",
        "provider_id",
        "provider_key",
        "relative_path",
        "resolved_at",
        "sha",
        "source_item_id",
        "source_uri",
        "space_key",
        "status",
        "suffix",
    }
)

__all__ = [
    "VectorEvidenceRecord",
    "VectorPayload",
    "VectorProjectionBatch",
    "VectorProjectionManifest",
]


class GraphEdgeRecord(StrictModel):
    """One deterministic structural or source-explicit graph relation."""

    relation_id: str = Field(min_length=1)
    relation_type: RelationType
    source_node_key: str = Field(min_length=1)
    target_node_key: str = Field(min_length=1)
    graph_schema_version: Literal["2.0"] = GRAPH_SCHEMA_VERSION
    ownership_scope: GraphOwnershipScope
    owner_id: TenantId
    source_scope_id: str | None = None
    document_id: DocumentId | None = None
    document_version_id: DocumentVersionId | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    source_relation_version: str = Field(min_length=1)
    source_explicit: bool

    @model_validator(mode="after")
    def validate_relation(self) -> GraphEdgeRecord:
        if self.source_node_key == self.target_node_key:
            raise ValueError("canonical relations must not self-reference")
        _validate_ownership(
            ownership_scope=self.ownership_scope,
            source_scope_id=self.source_scope_id,
            document_id=self.document_id,
            document_version_id=self.document_version_id,
            record="graph relationship",
        )
        required_scope = {
            RelationType.HAS_DATA_SOURCE: GraphOwnershipScope.SOURCE_SCOPE,
            RelationType.HAS_VERSION: GraphOwnershipScope.DOCUMENT_VERSION,
            RelationType.SUPPORTS: GraphOwnershipScope.DOCUMENT_VERSION,
            RelationType.RESOLVED_AT: GraphOwnershipScope.DOCUMENT_VERSION,
        }.get(self.relation_type)
        if required_scope is not None and self.ownership_scope != required_scope:
            raise ValueError(
                f"{self.relation_type.value} relationships require {required_scope.value} ownership"
            )
        _validate_attributes(self.attributes)
        return self


class GraphProjectionManifest(StrictModel):
    """Deterministic identity and checksum for one structural graph batch."""

    schema_version: Literal["2.0"] = GRAPH_SCHEMA_VERSION
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
    """Projection-neutral graph identity; content remains in the vector store."""

    node_key: str = Field(min_length=1)
    node_kind: KnowledgeNodeKind
    entity_type: GraphEntityType
    logical_id: str = Field(min_length=1)
    graph_schema_version: Literal["2.0"] = GRAPH_SCHEMA_VERSION
    ownership_scope: GraphOwnershipScope
    owner_id: TenantId
    source_scope_id: str | None = None
    document_id: DocumentId | None = None
    document_version_id: DocumentVersionId | None = None
    title: str | None = Field(default=None, max_length=512)
    section_path: tuple[str, ...] = ()
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("graph node text must be non-empty when supplied")
        return value

    @model_validator(mode="after")
    def validate_review_context(self) -> GraphNodeRecord:
        if any(not value.strip() for value in self.section_path):
            raise ValueError("graph node section path entries must be non-empty")
        expected_scope = {
            KnowledgeNodeKind.TENANT: GraphOwnershipScope.TENANT,
            KnowledgeNodeKind.DATA_SOURCE: GraphOwnershipScope.SOURCE_SCOPE,
            KnowledgeNodeKind.SOURCE_ENTITY: GraphOwnershipScope.SOURCE_SCOPE,
            KnowledgeNodeKind.DOCUMENT_VERSION: GraphOwnershipScope.DOCUMENT_VERSION,
            KnowledgeNodeKind.STRUCTURE: GraphOwnershipScope.DOCUMENT_VERSION,
            KnowledgeNodeKind.CHUNK: GraphOwnershipScope.DOCUMENT_VERSION,
        }[self.node_kind]
        if self.ownership_scope != expected_scope:
            raise ValueError(
                f"{self.node_kind.value} nodes require {expected_scope.value} ownership"
            )
        required_entity_type = {
            KnowledgeNodeKind.TENANT: GraphEntityType.TENANT,
            KnowledgeNodeKind.DATA_SOURCE: GraphEntityType.DATA_SOURCE,
            KnowledgeNodeKind.DOCUMENT_VERSION: GraphEntityType.DOCUMENT_VERSION,
            KnowledgeNodeKind.CHUNK: GraphEntityType.CHUNK,
        }.get(self.node_kind)
        if required_entity_type is not None and self.entity_type != required_entity_type:
            raise ValueError(
                f"{self.node_kind.value} nodes require entity_type={required_entity_type.value}"
            )
        _validate_ownership(
            ownership_scope=self.ownership_scope,
            source_scope_id=self.source_scope_id,
            document_id=self.document_id,
            document_version_id=self.document_version_id,
            record="graph node",
        )
        _validate_attributes(self.attributes)
        if self.node_kind == KnowledgeNodeKind.CHUNK and self.node_key != self.logical_id:
            raise ValueError("chunk graph node_key must equal its exact chunk ID")
        if self.entity_type in {
            GraphEntityType.LOCAL_ROOT,
            GraphEntityType.LOCAL_DIRECTORY,
            GraphEntityType.LOCAL_FILE,
        }:
            for key in ("path", "relative_path", "parent_path", "parent_relative_path"):
                value = self.attributes.get(key)
                if isinstance(value, str) and value.startswith(("/", "\\")):
                    raise ValueError("local graph paths must be portable relative paths")
        return self


def _validate_ownership(
    *,
    ownership_scope: GraphOwnershipScope,
    source_scope_id: str | None,
    document_id: DocumentId | None,
    document_version_id: DocumentVersionId | None,
    record: str,
) -> None:
    if source_scope_id is not None and not source_scope_id.strip():
        raise ValueError(f"{record} source_scope_id must be non-empty when supplied")
    version_owned = ownership_scope == GraphOwnershipScope.DOCUMENT_VERSION
    if version_owned and (document_id is None or document_version_id is None):
        raise ValueError(f"version-owned {record} requires document and version IDs")
    if not version_owned and (document_id is not None or document_version_id is not None):
        raise ValueError(f"only version-owned {record} may carry document and version IDs")
    if ownership_scope == GraphOwnershipScope.TENANT and source_scope_id is not None:
        raise ValueError(f"tenant-owned {record} must not carry source_scope_id")
    if ownership_scope != GraphOwnershipScope.TENANT and source_scope_id is None:
        raise ValueError(f"non-tenant {record} requires source_scope_id")


def _validate_attributes(attributes: Mapping[str, Any]) -> None:
    _validate_attribute_mapping(attributes, depth=0)


def _validate_attribute_mapping(attributes: Mapping[str, Any], *, depth: int) -> None:
    if len(attributes) > _MAX_GRAPH_ATTRIBUTES:
        raise ValueError(f"graph attributes may contain at most {_MAX_GRAPH_ATTRIBUTES} entries")
    reject_runtime_fields(attributes)
    for key, value in attributes.items():
        normalized = str(key).strip().casefold().replace("-", "_")
        if not normalized or len(normalized) > _MAX_ATTRIBUTE_KEY_LENGTH:
            raise ValueError("graph attribute keys must be bounded non-empty text")
        tokens = set(normalized.split("_"))
        if normalized in _FORBIDDEN_ATTRIBUTE_TOKENS or tokens & _FORBIDDEN_ATTRIBUTE_TOKENS:
            raise ValueError(f"graph attribute field is not allowed: {key}")
        if normalized not in _ALLOWED_ATTRIBUTE_FIELDS:
            raise ValueError(f"graph attribute field is not allowlisted: {key}")
        _validate_attribute_value(value, depth=depth)


def _validate_attribute_value(value: Any, *, depth: int) -> None:
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, str):
        if len(value) > _MAX_ATTRIBUTE_TEXT_LENGTH:
            raise ValueError("graph attribute text exceeds the bounded metadata limit")
        return
    if isinstance(value, Mapping):
        if depth >= 1 or len(value) > _MAX_GRAPH_ATTRIBUTES:
            raise ValueError("graph attribute mappings must be shallow and bounded")
        _validate_attribute_mapping(value, depth=depth + 1)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > _MAX_ATTRIBUTE_SEQUENCE:
            raise ValueError("graph attribute sequences exceed the bounded metadata limit")
        for item in value:
            _validate_attribute_value(item, depth=depth + 1)
        return
    raise ValueError("graph attributes support JSON-compatible metadata values only")


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
            self.missing_chunk_ids
            or self.invalid_source_item_ids
            or self.content_field_records
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
