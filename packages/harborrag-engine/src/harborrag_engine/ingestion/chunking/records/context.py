from __future__ import annotations

from collections.abc import Mapping, Sequence

from harborrag_core.chunking import (
    ChunkHierarchy,
    CitationLocator,
    TableChunkLocator,
)
from harborrag_core.schemas.ids import ChunkId

from ..identity import ChunkIdentity, ChunkIdentityBuilder
from ..schemas import ChunkCandidate, ChunkingRequest


class ChunkContextBuilder:
    """Build structural, citation, and table context for a canonical chunk."""

    def __init__(self, identity_builder: ChunkIdentityBuilder) -> None:
        self._identity = identity_builder

    def hierarchy(
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
            parent_title=self.parent_title(candidate.metadata),
            previous_chunk_id=(
                ChunkId(previous.logical_chunk_id) if previous is not None else None
            ),
            next_chunk_id=(ChunkId(next_.logical_chunk_id) if next_ is not None else None),
        )

    @staticmethod
    def citation_locator(
        request: ChunkingRequest,
        candidate: ChunkCandidate,
    ) -> CitationLocator:
        span = candidate.source_span
        return CitationLocator(
            uri=request.document.provenance.url,
            start_offset=span.start_offset,
            end_offset=span.end_offset,
            start_line=span.start_line,
            end_line=span.end_line,
            page_start=span.page_start,
            page_end=span.page_end,
            source_element_ids=span.element_ids,
        )

    def table_locator(
        self,
        *,
        request: ChunkingRequest,
        candidate: ChunkCandidate,
        content_hash: str,
        is_table: bool,
    ) -> TableChunkLocator | None:
        if not is_table:
            return None
        supplied_table_id = candidate.metadata.get("table_id")
        table_id = (
            str(supplied_table_id)
            if supplied_table_id is not None and str(supplied_table_id).strip()
            else self._identity.table_id(
                document_id=request.document.id,
                section_path=candidate.structural_path,
                stable_table_location={"anchor": candidate.anchor},
            )
        )
        supplied_version_id = candidate.metadata.get("table_version_id")
        lines = candidate.content.splitlines()
        row_start = candidate.metadata.get("row_start", 0)
        row_end = candidate.metadata.get("row_end", max(len(lines) - 1, 0))
        return TableChunkLocator(
            table_id=table_id,
            table_version_id=(
                str(supplied_version_id)
                if supplied_version_id is not None and str(supplied_version_id).strip()
                else self._identity.table_version_id(
                    table_id=table_id,
                    source_version=request.document_version_id,
                    content_hash=content_hash,
                )
            ),
            row_start=row_start if isinstance(row_start, int) else 0,
            row_end=row_end if isinstance(row_end, int) else max(len(lines) - 1, 0),
            column_count=max((len(line.split("\t")) for line in lines), default=1),
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

    @staticmethod
    def parent_title(metadata: Mapping[str, object]) -> str | None:
        values = metadata.get("ancestor_titles") or metadata.get("breadcrumb")
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
            for value in reversed(values):
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None
