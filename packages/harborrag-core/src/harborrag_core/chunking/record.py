from __future__ import annotations

from datetime import datetime
from typing import Any, Self

from pydantic import Field, model_validator

from harborrag_core.base import StrictModel
from harborrag_core.schemas.ids import ChunkId, DocumentId, DocumentVersionId, TenantId

from .metadata import ChunkMetadata, FrozenMetadata
from .schemas import (
    ChunkContext,
    ChunkHierarchy,
    ChunkKind,
    ChunkQuality,
    ChunkRelation,
    ConnectorType,
    DocumentKind,
)
from .source_schemas import ChunkSecurity, SourceAttribute, SourceLocator
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
    chunk_kind: ChunkKind

    tenant_id: TenantId
    connection_id: str = Field(min_length=1)
    source_scope: str = Field(min_length=1)
    source_item_id: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    document_id: DocumentId
    document_version_id: DocumentVersionId
    ordinal: int = Field(ge=0)

    content: str
    contextual_prefix: str = ""
    embedding_text: str = Field(min_length=1)
    search_text: str = Field(min_length=1)
    token_count: int = Field(ge=0)
    language: str | None = None

    source_locator: SourceLocator = Field(default_factory=SourceLocator)
    hierarchy: ChunkHierarchy = Field(default_factory=ChunkHierarchy)
    security: ChunkSecurity
    relations: tuple[ChunkRelation, ...] = ()
    quality: ChunkQuality = Field(default_factory=ChunkQuality)
    table_locator: TableChunkLocator | None = None
    source_attributes: tuple[SourceAttribute, ...] = ()

    metadata: ChunkMetadata = Field(default_factory=FrozenMetadata)
    created_at: datetime | None = None

    @model_validator(mode="after")
    def validate_chunk(self) -> ChunkRecord:
        evidence_kinds = {
            ChunkKind.EVIDENCE,
            ChunkKind.TABLE,
            ChunkKind.CODE,
            ChunkKind.COMMENT,
            ChunkKind.EVENT,
            ChunkKind.JIRA_FIELD,
        }
        if self.chunk_kind in evidence_kinds and not self.content.strip():
            raise ValueError(f"content must be non-empty for {self.chunk_kind.value} chunks")
        expected_embedding = (
            f"{self.contextual_prefix}\n\n{self.content}"
            if self.contextual_prefix
            else self.content
        )
        if self.embedding_text != expected_embedding:
            raise ValueError("embedding_text must equal contextual_prefix plus content")
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
        if self.created_at is not None and self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware when provided")
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

    @property
    def chunk_revision_id(self) -> ChunkId:
        """Compatibility access for consumers using the former exact-ID name."""

        return self.chunk_id

    @property
    def artifact_id(self) -> str:
        """Compatibility access for the former ingestion artifact identity."""

        return self.source_item_id

    @property
    def artifact_revision_id(self) -> str:
        """Compatibility access for the former ingestion artifact version."""

        return self.source_version

    @property
    def role(self) -> str:
        """Compatibility access for strategy roles persisted before chunk_kind."""

        value = self.metadata.get("legacy_role")
        return value if isinstance(value, str) else self.chunk_kind.value

    @property
    def source_span(self) -> SourceLocator | None:
        """Compatibility access for the former source-span field."""

        return self.source_locator

    @property
    def context(self) -> ChunkContext:
        """Compatibility access for the former retrieval-context field."""

        return ChunkContext(
            title=self.hierarchy.document_title,
            structural_path=self.hierarchy.section_path,
            parent_title=self.hierarchy.parent_title,
            previous_chunk_id=self.hierarchy.previous_chunk_id,
            next_chunk_id=self.hierarchy.next_chunk_id,
        )

    # This boundary mirrors the former public constructor; a mapping would hide
    # migration fields and weaken validation at persisted-payload call sites.
    @classmethod
    def from_legacy(  # noqa: PLR0913
        cls,
        *,
        logical_chunk_id: str,
        chunk_revision_id: str,
        tenant_id: str,
        document_id: str,
        document_version_id: str,
        artifact_id: str,
        artifact_revision_id: str,
        ordinal: int,
        role: str,
        content: str,
        content_hash: str,
        token_count: int | None = None,
        source_span: SourceLocator | None = None,
        context: ChunkContext | None = None,
        metadata: dict[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> Self:
        """Migrate the former storage-shaped chunk constructor explicitly."""

        legacy_metadata = dict(metadata or {})
        legacy_metadata["legacy_role"] = role
        source_kind = str(legacy_metadata.get("source_kind") or "local").lower()
        connector_type = cls._legacy_connector_type(source_kind)
        chunk_kind = cls._legacy_chunk_kind(role)
        selected_context = context or ChunkContext()
        prefix = cls._legacy_contextual_prefix(selected_context)
        document_kind = cls._legacy_document_kind(connector_type, role)
        strategy_version = str(legacy_metadata.get("chunker_version") or "legacy")
        table_locator = (
            cls._legacy_table_locator(
                logical_chunk_id=logical_chunk_id,
                chunk_revision_id=chunk_revision_id,
                content=content,
                metadata=legacy_metadata,
            )
            if chunk_kind == ChunkKind.TABLE
            else None
        )
        return cls(
            strategy_version=strategy_version,
            chunk_id=ChunkId(chunk_revision_id),
            logical_chunk_id=ChunkId(logical_chunk_id),
            content_hash=content_hash,
            connector_type=connector_type,
            document_kind=document_kind,
            chunk_kind=chunk_kind,
            tenant_id=TenantId(tenant_id),
            connection_id=source_kind,
            source_scope=tenant_id,
            source_item_id=artifact_id,
            source_version=artifact_revision_id,
            document_id=DocumentId(document_id),
            document_version_id=DocumentVersionId(document_version_id),
            ordinal=ordinal,
            content=content,
            contextual_prefix=prefix,
            embedding_text=f"{prefix}\n\n{content}" if prefix else content,
            search_text=f"{document_id}\n{content}",
            token_count=token_count or 0,
            source_locator=(
                SourceLocator.model_validate(source_span.model_dump())
                if source_span is not None
                else SourceLocator()
            ),
            hierarchy=ChunkHierarchy(
                document_title=selected_context.title,
                section_path=selected_context.structural_path,
                parent_title=selected_context.parent_title,
                previous_chunk_id=selected_context.previous_chunk_id,
                next_chunk_id=selected_context.next_chunk_id,
            ),
            security=ChunkSecurity(permission_set_id=f"legacy:{tenant_id}"),
            table_locator=table_locator,
            metadata=legacy_metadata,
            created_at=created_at,
        )

    @staticmethod
    def _legacy_connector_type(source_kind: str) -> ConnectorType:
        if "confluence" in source_kind:
            return ConnectorType.CONFLUENCE
        if "jira" in source_kind:
            return ConnectorType.JIRA
        return ConnectorType.LOCAL

    @staticmethod
    def _legacy_chunk_kind(role: str) -> ChunkKind:
        normalized = role.lower()
        if normalized == "table":
            return ChunkKind.TABLE
        if "code" in normalized:
            return ChunkKind.CODE
        if "comment" in normalized:
            return ChunkKind.COMMENT
        if "event" in normalized:
            return ChunkKind.EVENT
        if normalized in {"jira.field", "jira_field"}:
            return ChunkKind.JIRA_FIELD
        return ChunkKind.EVIDENCE

    @staticmethod
    def _legacy_document_kind(connector: ConnectorType, role: str) -> DocumentKind:
        if "attachment" in role.lower():
            return DocumentKind.ATTACHMENT
        if connector == ConnectorType.CONFLUENCE:
            return DocumentKind.CONFLUENCE_PAGE
        if connector == ConnectorType.JIRA:
            return DocumentKind.JIRA_ISSUE
        return DocumentKind.LOCAL_FILE

    @staticmethod
    def _legacy_contextual_prefix(context: ChunkContext) -> str:
        lines: list[str] = []
        if context.title:
            lines.append(f"Document: {context.title}")
        if context.structural_path:
            lines.append(f"Section: {' > '.join(context.structural_path)}")
        return "\n".join(lines)

    @staticmethod
    def _legacy_table_locator(
        *,
        logical_chunk_id: str,
        chunk_revision_id: str,
        content: str,
        metadata: dict[str, Any],
    ) -> TableChunkLocator:
        lines = content.splitlines()
        column_count = max((len(line.split("\t")) for line in lines), default=1)
        row_start = metadata.get("table_row_start", metadata.get("row_start", 0))
        row_end = metadata.get(
            "table_row_end",
            metadata.get("row_end", max(len(lines) - 1, 0)),
        )
        return TableChunkLocator(
            table_id=str(metadata.get("table_id") or f"legacy-table:{logical_chunk_id}"),
            table_version_id=str(
                metadata.get("table_version_id") or f"legacy-table-version:{chunk_revision_id}"
            ),
            row_start=row_start if isinstance(row_start, int) else 0,
            row_end=row_end if isinstance(row_end, int) else max(len(lines) - 1, 0),
            column_count=column_count,
        )
