from __future__ import annotations

import re
from typing import Any

from harborrag_core.schemas.documents import ChunkRecord

from ..config import IndexingConfig


class ContextCapsuleBuilder:
    """Create retrieval context without copying unrestricted chunk bodies."""

    _whitespace = re.compile(r"\s+")

    def build(
        self,
        record: ChunkRecord,
        *,
        generation_id: str,
        vector_point_id: str | None,
        content_reference: str | None,
        config: IndexingConfig,
    ) -> dict[str, Any]:
        """Build bounded graph metadata for one canonical chunk."""

        normalized = self._whitespace.sub(" ", record.content).strip()
        preview = normalized[: config.capsule_maximum_characters]
        return {
            "logical_chunk_id": str(record.logical_chunk_id),
            "chunk_revision_id": str(record.chunk_revision_id),
            "title": record.context.title,
            "section_path": list(record.structural_path),
            "chunk_role": record.role,
            "preview": preview,
            "preview_truncated": len(normalized) > len(preview),
            "token_count": record.token_count or 0,
            "page_range": self._range(record.page_start, record.page_end),
            "line_range": self._range(record.start_line, record.end_line),
            "content_hash": record.content_hash,
            "vector_point_id": vector_point_id,
            "content_reference": content_reference,
            "generation_id": generation_id,
        }

    @staticmethod
    def _range(start: int | None, end: int | None) -> list[int] | None:
        return [start, end] if start is not None and end is not None else None
