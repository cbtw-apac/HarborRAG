from __future__ import annotations

from datetime import UTC, datetime

from harborrag_core.chunking import (
    ChunkHierarchy,
    ChunkKind,
    ChunkQuality,
    ChunkRecord,
    ChunkSecurity,
    ConnectorType,
    DocumentKind,
    SourceLocator,
)


def chunk_values(**changes: object) -> dict[str, object]:
    content = str(changes.get("content", "Canonical evidence"))
    prefix = str(
        changes.get(
            "contextual_prefix",
            "Document: HarborRAG\nSection: Architecture > Chunking",
        )
    )
    values: dict[str, object] = {
        "schema_version": "1.0",
        "strategy_version": "strategy-1",
        "chunk_id": "chunk:exact",
        "logical_chunk_id": "logical-chunk:stable",
        "content_hash": "sha256:content",
        "connector_type": ConnectorType.CONFLUENCE,
        "document_kind": DocumentKind.CONFLUENCE_PAGE,
        "chunk_kind": ChunkKind.EVIDENCE,
        "tenant_id": "tenant-1",
        "connection_id": "connection-1",
        "source_scope": "SPACE",
        "source_item_id": "page-123",
        "source_version": "7",
        "document_id": "document-1",
        "document_version_id": "document-version-7",
        "ordinal": 2,
        "content": content,
        "contextual_prefix": prefix,
        "embedding_text": f"{prefix}\n\n{content}" if prefix else content,
        "search_text": "page-123 HARBOR-7 Canonical evidence",
        "token_count": 2,
        "language": "en",
        "source_locator": SourceLocator(
            uri="https://example.test/pages/123",
            start_offset=10,
            end_offset=28,
            source_element_ids=("paragraph-1",),
        ),
        "hierarchy": ChunkHierarchy(
            document_title="HarborRAG",
            section_path=("Architecture", "Chunking"),
            section_id="section:chunking",
            parent_section_id="section:architecture",
            ancestry=("section:architecture",),
        ),
        "security": ChunkSecurity(permission_set_id="permission-set:1"),
        "relations": (),
        "quality": ChunkQuality(score=0.95),
        "source_attributes": (),
        "created_at": datetime(2026, 7, 29, tzinfo=UTC),
    }
    values.update(changes)
    return values


def make_chunk(**changes: object) -> ChunkRecord:
    return ChunkRecord(**chunk_values(**changes))  # type: ignore[arg-type]
