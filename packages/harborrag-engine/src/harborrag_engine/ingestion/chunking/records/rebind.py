from __future__ import annotations

from collections.abc import Sequence

from harborrag_core.chunking import CanonicalIdentityBuilder, ChunkRecord
from harborrag_core.schemas.ids import ChunkId, DocumentVersionId


class ChunkVersionRebinder:
    """Rebind unchanged canonical chunks to a new processing version."""

    def __init__(self, identities: CanonicalIdentityBuilder | None = None) -> None:
        self._identities = identities or CanonicalIdentityBuilder()

    def rebind(
        self,
        chunks: Sequence[ChunkRecord],
        *,
        document_version_id: str,
    ) -> tuple[ChunkRecord, ...]:
        if not chunks:
            raise ValueError("chunk rebinding requires an existing chunk set")
        if len({chunk.document_id for chunk in chunks}) != 1:
            raise ValueError("chunk rebinding requires one document")
        target = DocumentVersionId(document_version_id)
        return tuple(
            chunk.model_copy(
                update={
                    "document_version_id": target,
                    "chunk_id": ChunkId(
                        self._identities.chunk_id(
                            logical_chunk_id=str(chunk.logical_chunk_id),
                            document_version_id=document_version_id,
                            strategy_version=chunk.strategy_version,
                            content_hash=chunk.content_hash,
                        )
                    ),
                }
            )
            for chunk in chunks
        )
