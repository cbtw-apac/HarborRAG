from __future__ import annotations

from dataclasses import dataclass

from harborrag_core.chunking import (
    ChunkContainer,
    ChunkHierarchy,
    ChunkKind,
    ChunkQuality,
    ChunkRecord,
    ChunkSecurity,
    ConnectorType,
    ContainerKind,
    DocumentKind,
    SourceAttribute,
    TableChunkLocator,
    content_fingerprint,
    encoded_identifier,
)
from harborrag_core.contracts import TokenCounter
from harborrag_core.schemas.ids import ChunkId, DocumentId, DocumentVersionId, TenantId

from ..config import ChunkingPlan
from ..identity import ChunkIdentityBuilder
from .errors import TableChunkingError
from .fragmentation import TokenBudgetFragmenter
from .models import (
    PlannedTableChunk,
    TableChunkingRequest,
    TableChunkRole,
    TableClassification,
)
from .rendering import TableRenderer


@dataclass(frozen=True, slots=True)
class _TableFragment:
    content: str
    contextual_prefix: str
    search_text: str
    ordinal: int
    index: int
    count: int


class TableChunkFactory:
    """Create canonical ChunkRecord values from exact planned table views."""

    def __init__(self, token_counter: TokenCounter) -> None:
        self._token_counter = token_counter
        self._identity = ChunkIdentityBuilder()
        self._fragmenter = TokenBudgetFragmenter(token_counter)

    def build(
        self,
        request: TableChunkingRequest,
        classification: TableClassification,
        planned: PlannedTableChunk,
        plan: ChunkingPlan,
        *,
        ordinal_start: int,
    ) -> tuple[ChunkRecord, ...]:
        artifact = request.artifact
        renderer = TableRenderer(artifact)
        content = renderer.render(
            classification,
            planned,
            plan.table_policy.route_preview_rows,
        )
        prefix = self._contextual_prefix(request, planned)
        fragments = self._fragmenter.split(
            content,
            prefix,
            plan.hard_maximum_tokens,
            repeat_header=planned.role == TableChunkRole.EVIDENCE,
        )
        search_text = self._search_text(request, renderer, planned)
        return tuple(
            self._record(
                request,
                classification,
                planned,
                plan,
                _TableFragment(
                    content=fragment,
                    contextual_prefix=prefix,
                    search_text=search_text,
                    ordinal=ordinal_start + index,
                    index=index,
                    count=len(fragments),
                ),
            )
            for index, fragment in enumerate(fragments)
        )

    def _record(
        self,
        request: TableChunkingRequest,
        classification: TableClassification,
        planned: PlannedTableChunk,
        plan: ChunkingPlan,
        fragment: _TableFragment,
    ) -> ChunkRecord:
        artifact = request.artifact
        content_hash = content_fingerprint(fragment.content)
        anchor = (
            f"table:{artifact.table_id}/{planned.role.value}/"
            f"{planned.projection_type.value}/rows:{planned.row_start}:{planned.row_end}/"
            f"columns:{','.join(str(value) for value in planned.selected_column_indices)}/"
            f"fragment:{fragment.index}"
        )
        identity = self._identity.identify(
            document_id=artifact.document_id,
            document_version_id=artifact.document_version_id,
            strategy_version=plan.strategy_version,
            section_path=artifact.section_path,
            structural_anchor=anchor,
            local_part_index=fragment.index,
            chunk_kind=ChunkKind.TABLE,
            content_hash=content_hash,
        )
        embedding_text = (
            f"{fragment.contextual_prefix}\n\n{fragment.content}"
            if fragment.contextual_prefix
            else fragment.content
        )
        token_count = self._token_counter.count(embedding_text)
        if token_count > plan.hard_maximum_tokens:
            raise TableChunkingError(
                f"table {artifact.table_id!r} fragment exceeds hard token limit"
            )
        issue_values = tuple(
            dict.fromkeys(
                (
                    *classification.warnings,
                    *(("fragmented oversized table view",) if fragment.count > 1 else ()),
                )
            )
        )
        return ChunkRecord(
            strategy_version=plan.strategy_version,
            chunk_id=ChunkId(identity.chunk_id),
            logical_chunk_id=ChunkId(identity.logical_chunk_id),
            content_hash=content_hash,
            connector_type=ConnectorType.CONFLUENCE,
            document_kind=DocumentKind.CONFLUENCE_PAGE,
            chunk_kind=ChunkKind.TABLE,
            tenant_id=TenantId(request.tenant_id),
            connection_id=request.connection_id,
            source_scope=request.source_scope,
            source_item_id=artifact.document_id,
            source_version=artifact.source_version,
            document_id=DocumentId(artifact.document_id),
            document_version_id=DocumentVersionId(artifact.document_version_id),
            ordinal=fragment.ordinal,
            content=fragment.content,
            contextual_prefix=fragment.contextual_prefix,
            embedding_text=embedding_text,
            search_text=fragment.search_text,
            token_count=token_count,
            source_locator=artifact.source_locator,
            hierarchy=self._hierarchy(request),
            security=ChunkSecurity(
                permission_set_id=self._identity.permission_set_id(
                    tenant_id=request.tenant_id,
                    permissions=request.permissions,
                ),
                inherited_from_document_id=DocumentId(artifact.document_id),
            ),
            quality=ChunkQuality(
                score=0.95 if planned.role == TableChunkRole.EVIDENCE else 1.0,
                is_complete=fragment.count == 1,
                issues=issue_values,
            ),
            table_locator=TableChunkLocator(
                table_id=artifact.table_id,
                table_version_id=artifact.table_version_id,
                row_start=planned.row_start,
                row_end=planned.row_end,
                column_count=artifact.column_count,
                key_column_indices=planned.repeated_key_column_indices,
                selected_column_indices=planned.selected_column_indices,
                selected_columns=tuple(
                    artifact.column_names[index] for index in planned.selected_column_indices
                ),
                repeated_header_row_count=planned.repeated_header_row_count,
                projection_type=planned.projection_type,
                tab_path=artifact.tab_path,
                fragment_index=(fragment.index if fragment.count > 1 else None),
                fragment_count=(fragment.count if fragment.count > 1 else None),
            ),
            source_attributes=(
                SourceAttribute(key="table_id", value=artifact.table_id),
                SourceAttribute(key="table_shape", value=classification.shape.value),
                SourceAttribute(key="table_chunk_role", value=planned.role.value),
            ),
            metadata={
                "table_chunk_role": planned.role.value,
                "table_shape": classification.shape.value,
                "classification_confidence": classification.confidence,
                "projection_type": planned.projection_type.value,
                "table_content_hash": artifact.content_hash,
                "key_column_confidences": dict(classification.key_column_confidences),
                "time_column_index": classification.time_column_index,
                "profile_name": plan.profile,
                "strategy_version": plan.strategy_version,
                "fragment_index": fragment.index,
                "fragment_count": fragment.count,
            },
        )

    def _hierarchy(self, request: TableChunkingRequest) -> ChunkHierarchy:
        artifact = request.artifact
        section_id = self._identity.section_id(
            document_id=artifact.document_id,
            section_path=(*artifact.tab_path, *artifact.section_path),
        )
        ancestry = tuple(
            self._identity.section_id(
                document_id=artifact.document_id,
                section_path=(*artifact.tab_path, *artifact.section_path[:depth]),
            )
            for depth in range(1, len(artifact.section_path))
        )
        containers = tuple(
            ChunkContainer(
                container_id=encoded_identifier(
                    "table-tab",
                    {
                        "document_id": artifact.document_id,
                        "table_id": artifact.table_id,
                        "path": artifact.tab_path[: index + 1],
                    },
                ),
                kind=ContainerKind.TAB,
                ordinal=index,
                title=title,
            )
            for index, title in enumerate(artifact.tab_path)
        )
        return ChunkHierarchy(
            document_title=request.page_title,
            section_path=artifact.section_path,
            section_id=section_id,
            parent_section_id=ancestry[-1] if ancestry else None,
            ancestry=ancestry,
            containers=containers,
            parent_title=(artifact.section_path[-1] if artifact.section_path else None),
        )

    @staticmethod
    def _contextual_prefix(
        request: TableChunkingRequest,
        planned: PlannedTableChunk,
    ) -> str:
        artifact = request.artifact
        lines = [
            "Connector: Confluence",
            f"Space: {request.space}",
            f"Page: {request.page_title}",
        ]
        if artifact.tab_path:
            lines.append(f"Tab: {' > '.join(artifact.tab_path)}")
        if artifact.section_path:
            lines.append(f"Section: {' > '.join(artifact.section_path)}")
        if artifact.caption:
            lines.append(f"Table: {artifact.caption}")
        if planned.role == TableChunkRole.EVIDENCE:
            lines.append(f"Rows: {planned.row_start}–{planned.row_end}")
        return "\n".join(lines)

    @staticmethod
    def _search_text(
        request: TableChunkingRequest,
        renderer: TableRenderer,
        planned: PlannedTableChunk,
    ) -> str:
        exact = renderer.search_text(
            range(planned.row_start, planned.row_end + 1),
            planned.selected_column_indices,
        )
        return "\n".join(
            (
                request.artifact.document_id,
                request.artifact.table_id,
                request.page_title,
                request.space,
                *request.artifact.section_path,
                *request.artifact.tab_path,
                exact,
            )
        )
