from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from harborrag_core.chunking import ChunkRecord
from harborrag_core.indexing import SparseVector
from harborrag_core.ingestion import ChunkRepresentation, RepresentationSet
from harborrag_core.models.embed import (
    EmbeddingPurpose,
    HarborEmbedRequest,
)
from harborrag_core.ports.model_clients import AsyncHarborEmbedClientProtocol

from .sparse import BM25SparseEncoder


@dataclass(frozen=True, slots=True)
class RepresentationEncodingPolicy:
    logical_model: str
    dense_profile_id: str
    dense_dimension: int
    normalize_dense: bool = True
    batch_size: int = 32

    def __post_init__(self) -> None:
        if not self.logical_model.strip() or not self.dense_profile_id.strip():
            raise ValueError("dense model and profile IDs must be non-empty")
        if self.dense_dimension < 1 or self.batch_size < 1:
            raise ValueError("dense dimension and batch size must be positive")


class ChunkRepresentationEncoder:
    """Encode canonical chunks without persisting provider runtime metadata."""

    def __init__(
        self,
        embed_client: AsyncHarborEmbedClientProtocol,
        sparse_encoder: BM25SparseEncoder,
        policy: RepresentationEncodingPolicy,
    ) -> None:
        self._embed = embed_client
        self._sparse = sparse_encoder
        self._policy = policy

    async def encode(
        self,
        chunks: Sequence[ChunkRecord],
    ) -> RepresentationSet:
        self.validate_chunks(chunks)
        dense_vectors = await self.encode_dense(chunks)
        sparse_vectors = self.encode_sparse(chunks)
        first = chunks[0]
        return RepresentationSet(
            document_id=first.document_id,
            document_version_id=first.document_version_id,
            dense_profile_id=self.dense_profile_id,
            sparse_profile_id=self.sparse_profile_id,
            dense_dimension=self.dense_dimension,
            records=tuple(
                ChunkRepresentation(
                    chunk_id=str(chunk.chunk_id),
                    dense_vector=dense_vectors[str(chunk.chunk_id)],
                    sparse_vector=sparse_vectors[str(chunk.chunk_id)],
                )
                for chunk in chunks
            ),
        )

    async def encode_dense(
        self,
        chunks: Sequence[ChunkRecord],
    ) -> dict[str, list[float]]:
        self.validate_chunks(chunks)
        dense_vectors: dict[str, list[float]] = {}
        for start in range(0, len(chunks), self._policy.batch_size):
            batch = tuple(chunks[start : start + self._policy.batch_size])
            response = await self._embed.aembed(
                request=HarborEmbedRequest(
                    inputs=tuple(chunk.embedding_text for chunk in batch),
                    logical_model=self._policy.logical_model,
                    dimensions=self._policy.dense_dimension,
                    purpose=EmbeddingPurpose.DOCUMENT,
                    normalize=self._policy.normalize_dense,
                    cacheable=True,
                    sensitive=True,
                )
            )
            values = self._vectors(response.embeddings, expected=len(batch))
            for chunk, vector in zip(batch, values, strict=True):
                dense_vectors[str(chunk.chunk_id)] = vector
        return dense_vectors

    def encode_sparse(self, chunks: Sequence[ChunkRecord]) -> dict[str, SparseVector]:
        self.validate_chunks(chunks)
        return {
            str(chunk.chunk_id): self._sparse.encode(chunk.search_text).vector for chunk in chunks
        }

    @staticmethod
    def validate_chunks(chunks: Sequence[ChunkRecord]) -> None:
        if not chunks:
            raise ValueError("representation encoding requires canonical chunks")
        if (
            len({chunk.document_id for chunk in chunks}) != 1
            or len({chunk.document_version_id for chunk in chunks}) != 1
        ):
            raise ValueError("representation chunks must belong to one document version")

    @property
    def dense_profile_id(self) -> str:
        return self._policy.dense_profile_id

    @property
    def sparse_profile_id(self) -> str:
        return self._sparse.profile.profile_id

    @property
    def dense_dimension(self) -> int:
        return self._policy.dense_dimension

    def _vectors(
        self,
        embeddings: Sequence[object],
        *,
        expected: int,
    ) -> tuple[list[float], ...]:
        by_index: dict[int, list[float]] = {}
        for fallback_index, embedding in enumerate(embeddings):
            raw_index = getattr(embedding, "index", fallback_index)
            value = getattr(embedding, "value", None)
            if not isinstance(raw_index, int) or not isinstance(value, tuple):
                raise ValueError("dense encoder returned a non-float embedding")
            if raw_index in by_index:
                raise ValueError("dense encoder returned duplicate input indices")
            vector = list(value)
            if len(vector) != self._policy.dense_dimension:
                raise ValueError("dense encoder returned an unexpected dimension")
            by_index[raw_index] = vector
        if set(by_index) != set(range(expected)):
            raise ValueError("dense encoder returned an incomplete result")
        return tuple(by_index[index] for index in range(expected))
