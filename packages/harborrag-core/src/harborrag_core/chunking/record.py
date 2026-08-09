from __future__ import annotations

from pydantic import Field, model_validator

from harborrag_core.base import StrictModel
from harborrag_core.schemas.ids import ChunkId, DocumentId, DocumentVersionId, TenantId

from .metadata import ChunkMetadata, FrozenMetadata
from .schemas import (
    ChunkHierarchy,
    ChunkKind,
    ChunkQuality,
    ChunkRelation,
    ConnectorType,
    DocumentKind,
    RecordKind,
)
from .source_schemas import ChunkSecurity, CitationLocator, SourceAttribute
from .table_schemas import TableChunkLocator


class ChunkRecord(StrictModel):
    """Canonical immutable, source-independent chunk revision."""

    schema_version: str = Field(default="1.0", min_length=1)
    strategy_version: str = Field(min_length=1)
    chunk_id: ChunkId
    logical_chunk_id: ChunkId
    content_hash: str = Field(min_length=1)

    connector_type: ConnectorType
    document_kind: DocumentKind
    record_kind: RecordKind
    chunk_kind: ChunkKind

    tenant_id: TenantId
    connection_id: str = Field(min_length=1)
    source_scope_id: str = Field(min_length=1)
    source_item_id: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    document_id: DocumentId
    document_version_id: DocumentVersionId
    ordinal: int = Field(ge=0)

    content: str
    embedding_text: str = Field(min_length=1)
    search_text: str = Field(min_length=1)
    token_count: int = Field(ge=0)
    language: str | None = None

    citation_locator: CitationLocator = Field(default_factory=CitationLocator)
    hierarchy: ChunkHierarchy = Field(default_factory=ChunkHierarchy)
    security: ChunkSecurity
    relations: tuple[ChunkRelation, ...] = ()
    quality: ChunkQuality = Field(default_factory=ChunkQuality)
    table_locator: TableChunkLocator | None = None
    source_attributes: tuple[SourceAttribute, ...] = ()

    metadata: ChunkMetadata = Field(default_factory=FrozenMetadata)

    @model_validator(mode="after")
    def validate_chunk(self) -> ChunkRecord:
        evidence_kinds = {
            ChunkKind.TEXT,
            ChunkKind.TABLE,
            ChunkKind.CODE,
            ChunkKind.COMMENT,
            ChunkKind.EVENT,
            ChunkKind.JIRA_FIELD,
        }
        if self.chunk_kind in evidence_kinds and not self.content.strip():
            raise ValueError(f"content must be non-empty for {self.chunk_kind.value} chunks")
        if not self.embedding_text.strip():
            raise ValueError("embedding_text must be non-empty")
        if not self.search_text.strip():
            raise ValueError("search_text must be non-empty")
        if self.chunk_kind == ChunkKind.TABLE and self.table_locator is None:
            raise ValueError("table chunks require table_locator")
        if self.chunk_kind != ChunkKind.TABLE and self.table_locator is not None:
            raise ValueError("table_locator is only valid for table chunks")
        if self.document_kind == DocumentKind.CONFLUENCE_PAGE and (
            self.connector_type != ConnectorType.CONFLUENCE
        ):
            raise ValueError("confluence_page requires the confluence connector")
        if self.document_kind == DocumentKind.JIRA_ISSUE and (
            self.connector_type != ConnectorType.JIRA
        ):
            raise ValueError("jira_issue requires the jira connector")
        if self.document_kind == DocumentKind.LOCAL_FILE and (
            self.connector_type != ConnectorType.LOCAL
        ):
            raise ValueError("local_file requires the local connector")
        self._validate_references()
        self._validate_collections()
        return self

    def _validate_references(self) -> None:
        own_ids = {str(self.chunk_id), str(self.logical_chunk_id)}
        references = (
            self.hierarchy.parent_chunk_id,
            self.hierarchy.previous_chunk_id,
            self.hierarchy.next_chunk_id,
        )
        if any(reference is not None and str(reference) in own_ids for reference in references):
            raise ValueError("chunk hierarchy must not self-reference a chunk identity")
        if any(relation.target_id in own_ids for relation in self.relations):
            raise ValueError("chunk relations must not self-reference a chunk identity")

    def _validate_collections(self) -> None:
        relation_keys = {
            (relation.relation_type, relation.target_id, relation.target_version_id)
            for relation in self.relations
        }
        if len(relation_keys) != len(self.relations):
            raise ValueError("chunk relations must not contain duplicates")
        attribute_keys = {attribute.key for attribute in self.source_attributes}
        if len(attribute_keys) != len(self.source_attributes):
            raise ValueError("source_attributes keys must be unique")
