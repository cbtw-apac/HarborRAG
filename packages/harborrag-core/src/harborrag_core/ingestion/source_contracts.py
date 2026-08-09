from __future__ import annotations

from pydantic import Field, model_validator

from harborrag_core.base import StrictModel
from harborrag_core.chunking import ConnectorType, RelationType
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId

from .states import BindingKind, SourceAdmissionDecision


class SourceBinding(StrictModel):
    """Bind an ingestible source object to its optional parent."""

    kind: BindingKind
    parent_source_item_id: str | None = None

    @model_validator(mode="after")
    def validate_parent(self) -> SourceBinding:
        if self.kind == BindingKind.ROOT and self.parent_source_item_id is not None:
            raise ValueError("root source bindings must not have a parent")
        if self.kind != BindingKind.ROOT and self.parent_source_item_id is None:
            raise ValueError("non-root source bindings require a parent")
        return self


class SourceIdentity(StrictModel):
    """Stable source identity used to derive one logical document identity."""

    connector_type: ConnectorType
    connection_id: str = Field(min_length=1)
    source_item_id: str = Field(min_length=1)
    source_scope_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    binding: SourceBinding = Field(default_factory=lambda: SourceBinding(kind=BindingKind.ROOT))


class ChangeFingerprints(StrictModel):
    """The four independent change fingerprints used by admission."""

    admission_change_key: str = Field(min_length=1)
    canonical_content_hash: str = Field(min_length=1)
    retrieval_metadata_hash: str = Field(min_length=1)
    processing_fingerprint: str = Field(min_length=1)


class SourceObjectVersion(StrictModel):
    source_item_id: str = Field(min_length=1)
    source_version: str = Field(min_length=1)


class SourceRelationDescriptor(StrictModel):
    relation_type: RelationType
    target_source_item_id: str = Field(min_length=1)
    source_relation_version: str = Field(min_length=1)


class AdmissionSnapshot(StrictModel):
    """Inexpensive, order-independent descriptors used before fetching content."""

    source_version: str = Field(min_length=1)
    comments: tuple[SourceObjectVersion, ...] = ()
    attachments: tuple[SourceObjectVersion, ...] = ()
    relations: tuple[SourceRelationDescriptor, ...] = ()


class ProcessingProfile(StrictModel):
    """Every provider-independent choice that can make projections stale."""

    parser_profile: str = Field(min_length=1)
    normalizer_version: str = Field(min_length=1)
    chunk_strategy: str = Field(min_length=1)
    dense_encoder_profile: str = Field(min_length=1)
    sparse_encoder_profile: str = Field(min_length=1)
    graph_projection_version: str = Field(min_length=1)
    vector_projection_schema: str = Field(default="vector-payload-v1", min_length=1)


class DiscoveredSourceItem(StrictModel):
    """One compact source descriptor recorded during an authoritative scan."""

    source_identity: SourceIdentity
    document_id: DocumentId
    source_version: str = Field(min_length=1)
    admission_change_key: str = Field(min_length=1)
    descriptor: dict[str, object] = Field(default_factory=dict)


class SourceItemRegistration(StrictModel):
    """Result of atomically recording one item in an authoritative scan."""

    decision: SourceAdmissionDecision
    previous_source_version: str | None = None
    previous_admission_change_key: str | None = None
    previous_descriptor: dict[str, object] | None = None


class StoredSourceItem(StrictModel):
    """Authoritative discovered descriptor used by document workflows."""

    source_identity: SourceIdentity
    document_id: DocumentId
    source_version: str = Field(min_length=1)
    admission_change_key: str = Field(min_length=1)
    descriptor: dict[str, object] = Field(default_factory=dict)
    active: bool = True


class ActiveSourceDocument(StrictModel):
    """Published target resolved from one source-explicit relation."""

    source_item_id: str = Field(min_length=1)
    source_scope_id: str = Field(min_length=1)
    document_id: DocumentId
    document_version_id: DocumentVersionId
    title: str | None = None
