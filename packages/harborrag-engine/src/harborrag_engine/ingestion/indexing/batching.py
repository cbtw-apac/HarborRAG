from __future__ import annotations

from .config import IndexingConfig
from .schemas import (
    ChunkDiffResult,
    EmbeddingBatch,
    PreparedEmbeddingInput,
)


class EmbeddingBatchPlanner:
    """Select non-reusable chunks and pack prepared text in manifest order."""

    def plan(
        self,
        diff: ChunkDiffResult,
        prepared: tuple[PreparedEmbeddingInput, ...],
        config: IndexingConfig,
    ) -> tuple[EmbeddingBatch, ...]:
        """Group embedding-required chunks into deterministic bounded batches."""

        by_revision = {str(item.record.chunk_revision_id): item for item in prepared}
        if len(by_revision) != len(prepared):
            raise ValueError("canonical chunk revision IDs must be unique")
        selected: list[PreparedEmbeddingInput] = []
        for entry in diff.for_embedding:
            if entry.current is None:
                raise ValueError("embedding diff entry has no current chunk")
            try:
                item = by_revision[entry.current.chunk_revision_id]
            except KeyError as exc:
                raise ValueError(
                    f"prepared chunk {entry.current.chunk_revision_id!r} is missing"
                ) from exc
            if (
                str(item.record.logical_chunk_id) != entry.logical_chunk_id
                or item.record.content_hash != entry.current.content_hash
            ):
                raise ValueError("prepared chunk does not match its diff reference")
            if item.token_count > config.maximum_embedding_batch_tokens:
                raise ValueError(
                    f"chunk {item.record.chunk_revision_id!s} exceeds embedding batch token limit"
                )
            selected.append(item)

        batches: list[EmbeddingBatch] = []
        current: list[PreparedEmbeddingInput] = []
        current_tokens = 0
        for item in selected:
            exceeds_items = len(current) >= config.embedding_batch_size
            exceeds_tokens = bool(
                current
                and current_tokens + item.token_count > config.maximum_embedding_batch_tokens
            )
            if exceeds_items or exceeds_tokens:
                batches.append(self._batch(len(batches), current, current_tokens))
                current = []
                current_tokens = 0
            current.append(item)
            current_tokens += item.token_count
        if current:
            batches.append(self._batch(len(batches), current, current_tokens))
        return tuple(batches)

    @staticmethod
    def _batch(
        ordinal: int,
        inputs: list[PreparedEmbeddingInput],
        total_tokens: int,
    ) -> EmbeddingBatch:
        return EmbeddingBatch(
            ordinal=ordinal,
            inputs=tuple(inputs),
            total_tokens=total_tokens,
        )
