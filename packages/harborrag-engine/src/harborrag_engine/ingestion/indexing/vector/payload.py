from __future__ import annotations

from typing import Any

from harborrag_core.schemas.documents import ChunkRecord

from ..config import IndexingConfig


class VectorPayloadBuilder:
    """Build bounded vector metadata without provider-specific fields."""

    def build(
        self,
        record: ChunkRecord,
        *,
        generation_id: str,
        content_reference: str | None,
        config: IndexingConfig,
    ) -> dict[str, Any]:
        """Build staged vector metadata from a canonical chunk record."""

        source_kind = record.metadata.get("source_kind", "unknown")
        if not isinstance(source_kind, str) or not source_kind.strip():
            source_kind = "unknown"
        span = record.source_span
        payload: dict[str, Any] = {
            "tenant_id": str(record.tenant_id),
            "chunk_revision_id": str(record.chunk_revision_id),
            "logical_chunk_id": str(record.logical_chunk_id),
            "artifact_id": record.artifact_id,
            "artifact_revision_id": record.artifact_revision_id,
            "generation_id": generation_id,
            "source_kind": source_kind,
            "chunk_role": record.role,
            "structural_path": list(record.context.structural_path),
            "page_range": self._range(
                span.page_start if span is not None else None,
                span.page_end if span is not None else None,
            ),
            "line_range": self._range(
                span.start_line if span is not None else None,
                span.end_line if span is not None else None,
            ),
            "content_hash": record.content_hash,
            "token_count": record.token_count or 0,
            "embedding_configuration_fingerprint": (config.embedding_configuration_fingerprint),
            "content_reference": self._content_reference(record, content_reference),
            "index_state": "staged",
            "is_active": False,
        }
        if config.include_chunk_content_in_vector_payload:
            payload["content"] = record.content
        return payload

    @staticmethod
    def _range(start: int | None, end: int | None) -> list[int] | None:
        return [start, end] if start is not None and end is not None else None

    @staticmethod
    def _content_reference(
        record: ChunkRecord,
        manifest_reference: str | None,
    ) -> str:
        if manifest_reference is not None and manifest_reference.strip():
            return manifest_reference.strip()
        for key in ("body_uri", "content_uri", "object_uri"):
            value = record.metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return f"harborrag:chunk:{record.chunk_revision_id!s}"
