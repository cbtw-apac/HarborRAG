from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from harborrag_core.chunking import (
    ChunkHierarchy,
    ChunkKind,
    ChunkQuality,
    ChunkRecord,
    ChunkRelation,
    ChunkSecurity,
    ConnectorType,
    DocumentKind,
    RelationType,
    SourceLocator,
    TableChunkLocator,
)
from harborrag_core.contracts.chunking import TokenCounter
from harborrag_core.schemas.ids import ChunkId, DocumentId, DocumentVersionId, TenantId

from .config import ChunkingProfile
from .identity import ChunkIdentity, ChunkIdentityBuilder
from .schemas import ChunkCandidate, ChunkingRequest


@dataclass(frozen=True, slots=True)
class CanonicalChunkInput:
    """Validated engine values required to build one canonical chunk."""

    request: ChunkingRequest
    candidate: ChunkCandidate
    identity: ChunkIdentity
    content_hash: str
    ordinal: int
    previous: ChunkIdentity | None
    next_: ChunkIdentity | None
    strategy_name: str
    strategy_version: str
    profile: ChunkingProfile
    configuration_hash: str
    contextualize_embeddings: bool


class CanonicalChunkFactory:
    """Translate one identity-bearing candidate into a canonical record."""

    def __init__(
        self,
        token_counter: TokenCounter,
        identity_builder: ChunkIdentityBuilder,
    ) -> None:
        self._token_counter = token_counter
        self._identity = identity_builder

    def build(self, values: CanonicalChunkInput) -> ChunkRecord:
        """Build one complete immutable chunk without mutating its candidate."""

        request = values.request
        candidate = values.candidate
        identity = values.identity
        source_locator = self._source_locator(request, candidate)
        hierarchy = self._hierarchy(
            request,
            candidate,
            identity,
            values.previous,
            values.next_,
        )
        contextual_prefix = (
            self._contextual_prefix(hierarchy) if values.contextualize_embeddings else ""
        )
        return ChunkRecord(
            strategy_version=values.strategy_version,
            chunk_id=ChunkId(identity.chunk_id),
            logical_chunk_id=ChunkId(identity.logical_chunk_id),
            content_hash=values.content_hash,
            connector_type=self._connector_type(request.source_kind),
            document_kind=self._document_kind(request.source_kind, candidate.role),
            chunk_kind=self.kind_for_role(candidate.role),
            tenant_id=TenantId(request.tenant_id),
            connection_id=request.source_kind,
            source_scope=request.tenant_id,
            source_item_id=request.artifact_id,
            source_version=request.artifact_revision_id,
            document_id=DocumentId(request.document.id),
            document_version_id=DocumentVersionId(request.artifact_revision_id),
            ordinal=values.ordinal,
            content=candidate.content,
            contextual_prefix=contextual_prefix,
            embedding_text=(
                f"{contextual_prefix}\n\n{candidate.content}"
                if contextual_prefix
                else candidate.content
            ),
            search_text=self._search_text(request, candidate.content),
            token_count=self._token_counter.count(candidate.content),
            source_locator=source_locator,
            hierarchy=hierarchy,
            security=ChunkSecurity(
                permission_set_id=self._identity.permission_set_id(
                    tenant_id=request.tenant_id,
                    permissions=request.document.provenance.permissions,
                )
            ),
            relations=self._relations(request),
            quality=ChunkQuality(),
            table_locator=self._table_locator(
                request=request,
                candidate=candidate,
                content_hash=values.content_hash,
            ),
            metadata=self._metadata(values),
        )

    @staticmethod
    def kind_for_role(role: str) -> ChunkKind:
        """Map existing structural strategy roles to stable chunk kinds."""

        if role == "table":
            return ChunkKind.TABLE
        if "code" in role:
            return ChunkKind.CODE
        if "comment" in role:
            return ChunkKind.COMMENT
        if "event" in role:
            return ChunkKind.EVENT
        if role in {"jira.field", "jira_field"}:
            return ChunkKind.JIRA_FIELD
        return ChunkKind.EVIDENCE

    def _hierarchy(
        self,
        request: ChunkingRequest,
        candidate: ChunkCandidate,
        identity: ChunkIdentity,
        previous: ChunkIdentity | None,
        next_: ChunkIdentity | None,
    ) -> ChunkHierarchy:
        parent_path = candidate.structural_path[:-1]
        return ChunkHierarchy(
            document_title=request.document.title.strip() or None,
            section_path=candidate.structural_path,
            section_id=identity.section_id,
            parent_section_id=(
                self._identity.section_id(
                    document_id=request.document.id,
                    section_path=parent_path,
                )
                if parent_path
                else None
            ),
            ancestry=self._section_ancestry(
                request.document.id,
                candidate.structural_path,
            ),
            parent_title=self._parent_title(candidate.metadata),
            previous_chunk_id=(
                ChunkId(previous.logical_chunk_id) if previous is not None else None
            ),
            next_chunk_id=(ChunkId(next_.logical_chunk_id) if next_ is not None else None),
        )

    @staticmethod
    def _source_locator(
        request: ChunkingRequest,
        candidate: ChunkCandidate,
    ) -> SourceLocator:
        span = candidate.source_span
        return SourceLocator(
            uri=request.document.provenance.url,
            start_offset=span.start_offset,
            end_offset=span.end_offset,
            start_line=span.start_line,
            end_line=span.end_line,
            page_start=span.page_start,
            page_end=span.page_end,
            source_element_ids=span.element_ids,
        )

    def _section_ancestry(
        self,
        document_id: str,
        section_path: tuple[str, ...],
    ) -> tuple[str, ...]:
        return tuple(
            self._identity.section_id(
                document_id=document_id,
                section_path=section_path[:depth],
            )
            for depth in range(1, len(section_path))
        )

    def _table_locator(
        self,
        *,
        request: ChunkingRequest,
        candidate: ChunkCandidate,
        content_hash: str,
    ) -> TableChunkLocator | None:
        if self.kind_for_role(candidate.role) != ChunkKind.TABLE:
            return None
        table_id = self._identity.table_id(
            document_id=request.document.id,
            section_path=candidate.structural_path,
            stable_table_location={"anchor": candidate.anchor},
        )
        lines = candidate.content.splitlines()
        row_start = candidate.metadata.get("row_start", 0)
        row_end = candidate.metadata.get("row_end", max(len(lines) - 1, 0))
        return TableChunkLocator(
            table_id=table_id,
            table_version_id=self._identity.table_version_id(
                table_id=table_id,
                source_version=request.artifact_revision_id,
                content_hash=content_hash,
            ),
            row_start=row_start if isinstance(row_start, int) else 0,
            row_end=row_end if isinstance(row_end, int) else max(len(lines) - 1, 0),
            column_count=max((len(line.split("\t")) for line in lines), default=1),
        )

    @staticmethod
    def _connector_type(source_kind: str) -> ConnectorType:
        if "confluence" in source_kind:
            return ConnectorType.CONFLUENCE
        if "jira" in source_kind:
            return ConnectorType.JIRA
        return ConnectorType.LOCAL

    @classmethod
    def _document_kind(cls, source_kind: str, role: str) -> DocumentKind:
        if "attachment" in role:
            return DocumentKind.ATTACHMENT
        connector = cls._connector_type(source_kind)
        if connector == ConnectorType.CONFLUENCE:
            return DocumentKind.CONFLUENCE_PAGE
        if connector == ConnectorType.JIRA:
            return DocumentKind.JIRA_ISSUE
        return DocumentKind.LOCAL_FILE

    @staticmethod
    def _contextual_prefix(hierarchy: ChunkHierarchy) -> str:
        lines: list[str] = []
        if hierarchy.document_title:
            lines.append(f"Document: {hierarchy.document_title}")
        if hierarchy.section_path:
            lines.append(f"Section: {' > '.join(hierarchy.section_path)}")
        return "\n".join(lines)

    @staticmethod
    def _search_text(request: ChunkingRequest, content: str) -> str:
        identifiers = [request.document.id]
        if request.document.provenance.record_id:
            identifiers.append(request.document.provenance.record_id)
        return "\n".join((*identifiers, content))

    @staticmethod
    def _relations(request: ChunkingRequest) -> tuple[ChunkRelation, ...]:
        supported = {relation.value: relation for relation in RelationType}
        return tuple(
            ChunkRelation(
                relation_type=supported[relation.predicate],
                target_id=relation.target_id,
            )
            for relation in request.document.relations
            if relation.predicate in supported
        )

    @staticmethod
    def _metadata(values: CanonicalChunkInput) -> dict[str, object]:
        return {
            **values.candidate.metadata,
            "boundary_kind": values.candidate.boundary_kind.value,
            "forced_split": values.candidate.forced_split,
            "structural_anchor": values.candidate.anchor,
            "local_part_index": values.candidate.local_part_index,
            "chunker_name": values.strategy_name,
            "chunker_version": values.strategy_version,
            "profile_name": values.profile.name,
            "configuration_hash": values.configuration_hash,
            "source_kind": values.request.source_kind,
            "legacy_role": values.candidate.role,
        }

    @staticmethod
    def _parent_title(metadata: Mapping[str, object]) -> str | None:
        values = metadata.get("ancestor_titles") or metadata.get("breadcrumb")
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
            for value in reversed(values):
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None
