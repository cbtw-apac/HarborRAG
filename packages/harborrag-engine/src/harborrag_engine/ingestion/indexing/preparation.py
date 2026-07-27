from __future__ import annotations

from collections.abc import Mapping

from harborrag_core.contracts.chunking import TokenCounter
from harborrag_core.schemas.documents import ChunkRecord

from .config import IndexingConfig
from .schemas import PreparedEmbeddingInput


class EmbeddingInputPreparer:
    """Keep canonical content immutable while adding small retrieval context."""

    def __init__(self, token_counter: TokenCounter) -> None:
        """Initialize the preparer with the configured token counter."""

        self._token_counter = token_counter

    def prepare(
        self,
        records: tuple[ChunkRecord, ...],
        config: IndexingConfig,
    ) -> tuple[PreparedEmbeddingInput, ...]:
        """Render canonical records into bounded embedding inputs."""

        return tuple(self._prepare(record, config) for record in records)

    def _prepare(
        self,
        record: ChunkRecord,
        config: IndexingConfig,
    ) -> PreparedEmbeddingInput:
        context = self._context(record)
        bounded = context[: config.embedding_context_maximum_characters].rstrip()
        text = f"{bounded}\n\n{record.content}" if bounded else record.content
        return PreparedEmbeddingInput(
            record=record,
            text=text,
            token_count=self._token_counter.count(text),
        )

    @classmethod
    def _context(cls, record: ChunkRecord) -> str:
        lines: list[str] = []
        cls._append(lines, "Title", record.context.title)
        if record.context.structural_path:
            lines.append(f"Section: {' > '.join(record.context.structural_path)}")
        cls._append(lines, "Role", record.role)
        metadata: Mapping[str, object] = record.metadata
        cls._append(lines, "Issue", metadata.get("issue_key"))
        cls._append(lines, "Issue summary", metadata.get("issue_summary"))
        cls._append(lines, "Page", metadata.get("page_title"))
        cls._append(
            lines,
            "File",
            metadata.get("file_path") or metadata.get("path"),
        )
        cls._append(
            lines,
            "Symbol",
            metadata.get("qualified_name") or metadata.get("symbol_name"),
        )
        return "\n".join(lines)

    @staticmethod
    def _append(lines: list[str], label: str, value: object) -> None:
        if isinstance(value, str) and value.strip():
            lines.append(f"{label}: {value.strip()}")
