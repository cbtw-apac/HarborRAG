"""Canonical graph node and relation records for the knowledge projection.

Split from ``projection_contracts`` for the same reason ``projection_vector`` is:
the graph and vector projections are independent contracts that happen to be
verified together, and only the verification records need both.
"""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from harborrag_core.base import StrictModel
from harborrag_core.chunking import RelationType
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId, TenantId

from .graph_attribute_validation import validate_graph_attributes
from .states import GraphEntityType, GraphOwnershipScope, KnowledgeNodeKind

GRAPH_SCHEMA_VERSION: Literal["2.0"] = "2.0"


_STRUCTURE_ENTITY_TYPES = frozenset(
    {
        GraphEntityType.SECTION,
        GraphEntityType.TABLE,
        GraphEntityType.COMMENT,
    }
)


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
        validate_graph_attributes(self.attributes)
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
        # STRUCTURE is the one open-ended kind whose types this engine fully owns:
        # the structural projector emits exactly these three, so anything else is a
        # mistake rather than an extension. SOURCE_ENTITY is deliberately left
        # unconstrained -- GraphEntityType is an open enum precisely so a new
        # connector can name its own item kinds without editing this contract.
        if (
            self.node_kind == KnowledgeNodeKind.STRUCTURE
            and self.entity_type not in _STRUCTURE_ENTITY_TYPES
        ):
            allowed = ", ".join(sorted(item.value for item in _STRUCTURE_ENTITY_TYPES))
            raise ValueError(f"structure nodes require entity_type in ({allowed})")
        _validate_ownership(
            ownership_scope=self.ownership_scope,
            source_scope_id=self.source_scope_id,
            document_id=self.document_id,
            document_version_id=self.document_version_id,
            record="graph node",
        )
        validate_graph_attributes(self.attributes)
        if self.node_kind == KnowledgeNodeKind.CHUNK and self.node_key != self.logical_id:
            raise ValueError("chunk graph node_key must equal its exact chunk ID")
        if self.entity_type in {
            GraphEntityType.LOCAL_ROOT,
            GraphEntityType.LOCAL_DIRECTORY,
            GraphEntityType.LOCAL_FILE,
        }:
            for key in ("path", "relative_path", "parent_path", "parent_relative_path"):
                value = self.attributes.get(key)
                if isinstance(value, str) and _is_host_specific_path(value):
                    raise ValueError("local graph paths must be portable relative paths")
        return self


def _is_host_specific_path(value: str) -> bool:
    """Detect a path that names a location on one host rather than a document.

    A leading separator is only one of the host-specific forms. Every Windows
    drive form is host-specific too: ``C:\\repo`` and ``C:/repo`` are rooted, and
    ``C:repo`` is relative to that drive's own working directory, so none of the
    three resolve to the same file anywhere else.
    """

    return (
        PurePosixPath(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
        or bool(PureWindowsPath(value).drive)
    )


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
