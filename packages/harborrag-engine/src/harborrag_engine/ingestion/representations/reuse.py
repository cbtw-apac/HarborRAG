from __future__ import annotations

from collections.abc import Sequence

from harborrag_core.chunking import ChunkRecord, RecordKind
from harborrag_core.ingestion import (
    ChunkRepresentation,
    RepresentationSet,
)

from .encoding import ChunkRepresentationEncoder


class RepresentationReuseService:
    """Reuse unchanged evidence vectors while always refreshing route records."""

    def __init__(self, encoder: ChunkRepresentationEncoder) -> None:
        self._encoder = encoder

    async def encode(
        self,
        chunks: Sequence[ChunkRecord],
        *,
        previous_chunks: Sequence[ChunkRecord] = (),
        previous_representations: RepresentationSet | None = None,
    ) -> RepresentationSet:
        if not chunks:
            raise ValueError("representation encoding requires canonical chunks")
        reusable = self._reusable(
            chunks,
            previous_chunks=previous_chunks,
            previous_representations=previous_representations,
        )
        pending = tuple(chunk for chunk in chunks if str(chunk.chunk_id) not in reusable)
        encoded: dict[str, ChunkRepresentation] = {}
        dense_profile_id: str
        sparse_profile_id: str
        dense_dimension: int
        if pending:
            generated = await self._encoder.encode(pending)
            encoded = {record.chunk_id: record for record in generated.records}
            dense_profile_id = generated.dense_profile_id
            sparse_profile_id = generated.sparse_profile_id
            dense_dimension = generated.dense_dimension
        elif previous_representations is not None:
            dense_profile_id = previous_representations.dense_profile_id
            sparse_profile_id = previous_representations.sparse_profile_id
            dense_dimension = previous_representations.dense_dimension
        else:  # pragma: no cover - pending is necessarily non-empty here
            raise RuntimeError("representation profiles are unavailable")
        records = tuple(
            reusable.get(str(chunk.chunk_id)) or encoded[str(chunk.chunk_id)] for chunk in chunks
        )
        return RepresentationSet(
            document_id=chunks[0].document_id,
            document_version_id=chunks[0].document_version_id,
            dense_profile_id=dense_profile_id,
            sparse_profile_id=sparse_profile_id,
            dense_dimension=dense_dimension,
            records=records,
        )

    async def reindex(
        self,
        chunks: Sequence[ChunkRecord],
        *,
        previous_chunks: Sequence[ChunkRecord],
        previous_representations: RepresentationSet,
        regenerate_dense: bool,
        regenerate_sparse: bool,
    ) -> RepresentationSet:
        """Regenerate only stale representation lanes for rebound chunks."""

        self._encoder.validate_chunks(chunks)
        previous_by_logical = {str(chunk.logical_chunk_id): chunk for chunk in previous_chunks}
        previous_vectors = {record.chunk_id: record for record in previous_representations.records}
        prior_for_new: dict[str, ChunkRepresentation] = {}
        for chunk in chunks:
            previous = previous_by_logical.get(str(chunk.logical_chunk_id))
            if previous is None:
                raise ValueError("rebound chunk has no prior logical identity")
            if (
                previous.embedding_text != chunk.embedding_text
                or previous.search_text != chunk.search_text
            ):
                raise ValueError("rebound chunk text changed without rechunking")
            representation = previous_vectors.get(str(previous.chunk_id))
            if representation is None:
                raise ValueError("prior representation set is incomplete")
            prior_for_new[str(chunk.chunk_id)] = representation

        dense = (
            await self._encoder.encode_dense(chunks)
            if regenerate_dense
            else {chunk_id: list(record.dense_vector) for chunk_id, record in prior_for_new.items()}
        )
        sparse = (
            self._encoder.encode_sparse(chunks)
            if regenerate_sparse
            else {chunk_id: record.sparse_vector for chunk_id, record in prior_for_new.items()}
        )
        return RepresentationSet(
            document_id=chunks[0].document_id,
            document_version_id=chunks[0].document_version_id,
            dense_profile_id=(
                self._encoder.dense_profile_id
                if regenerate_dense
                else previous_representations.dense_profile_id
            ),
            sparse_profile_id=(
                self._encoder.sparse_profile_id
                if regenerate_sparse
                else previous_representations.sparse_profile_id
            ),
            dense_dimension=(
                self._encoder.dense_dimension
                if regenerate_dense
                else previous_representations.dense_dimension
            ),
            records=tuple(
                ChunkRepresentation(
                    chunk_id=str(chunk.chunk_id),
                    dense_vector=dense[str(chunk.chunk_id)],
                    sparse_vector=sparse[str(chunk.chunk_id)],
                )
                for chunk in chunks
            ),
        )

    @staticmethod
    def _reusable(
        chunks: Sequence[ChunkRecord],
        *,
        previous_chunks: Sequence[ChunkRecord],
        previous_representations: RepresentationSet | None,
    ) -> dict[str, ChunkRepresentation]:
        if previous_representations is None or not previous_chunks:
            return {}
        prior_chunks = {str(chunk.logical_chunk_id): chunk for chunk in previous_chunks}
        prior_vectors = {record.chunk_id: record for record in previous_representations.records}
        reusable: dict[str, ChunkRepresentation] = {}
        for chunk in chunks:
            if chunk.record_kind == RecordKind.ROUTE:
                continue
            previous = prior_chunks.get(str(chunk.logical_chunk_id))
            if (
                previous is None
                or previous.embedding_text != chunk.embedding_text
                or previous.search_text != chunk.search_text
            ):
                continue
            representation = prior_vectors.get(str(previous.chunk_id))
            if representation is None:
                continue
            reusable[str(chunk.chunk_id)] = ChunkRepresentation(
                chunk_id=str(chunk.chunk_id),
                dense_vector=list(representation.dense_vector),
                sparse_vector=representation.sparse_vector,
            )
        return reusable
