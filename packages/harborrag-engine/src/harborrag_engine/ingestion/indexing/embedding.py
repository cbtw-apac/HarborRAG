from __future__ import annotations

import asyncio

from harborrag_core.models.embed import (
    EmbeddingPurpose,
    HarborEmbedMetadata,
    HarborEmbedRequest,
    HarborEmbedResponse,
)
from harborrag_core.ports.model_clients import AsyncHarborEmbedClientProtocol

from .config import IndexingConfig
from .errors import EmbeddingResultMismatchError
from .schemas import EmbeddedChunk, EmbeddingBatch, EmbeddingRun


class EmbeddingService:
    """Execute bounded batches without branching on the selected provider."""

    def __init__(self, client: AsyncHarborEmbedClientProtocol) -> None:
        """Initialize the service with a provider-neutral embedding client."""

        self._client = client

    async def embed(
        self,
        batches: tuple[EmbeddingBatch, ...],
        config: IndexingConfig,
    ) -> EmbeddingRun:
        """Embed all batches and preserve deterministic input ordering."""

        if not batches:
            return EmbeddingRun(
                chunks=(),
                configuration_fingerprint=(config.embedding_configuration_fingerprint),
                dimension=None,
                embedding_space=None,
            )

        semaphore = asyncio.Semaphore(config.embedding_concurrency)

        async def execute(batch: EmbeddingBatch) -> HarborEmbedResponse:
            async with semaphore:
                return await self._client.aembed(
                    request=HarborEmbedRequest(
                        inputs=tuple(item.text for item in batch.inputs),
                        logical_model=config.embedding_model,
                        dimensions=config.embedding_dimensions,
                        purpose=EmbeddingPurpose.DOCUMENT,
                        normalize=config.normalize_embeddings,
                        metadata=HarborEmbedMetadata(
                            document_ids=tuple(
                                dict.fromkeys(str(item.record.document_id) for item in batch.inputs)
                            ),
                            chunk_ids=tuple(
                                str(item.record.chunk_revision_id) for item in batch.inputs
                            ),
                            embedding_purpose=EmbeddingPurpose.DOCUMENT,
                        ),
                    )
                )

        responses = await asyncio.gather(*(execute(batch) for batch in batches))
        embedded: list[EmbeddedChunk] = []
        expected_space: str | None = None
        for batch, response in zip(batches, responses, strict=True):
            by_index = {item.index: item for item in response.embeddings}
            if len(by_index) != len(response.embeddings) or set(by_index) != set(
                range(len(batch.inputs))
            ):
                raise EmbeddingResultMismatchError(
                    f"embedding batch {batch.ordinal} returned incomplete or duplicate indices"
                )
            if expected_space is None:
                expected_space = response.embedding_space
            elif expected_space != response.embedding_space:
                raise EmbeddingResultMismatchError(
                    "embedding batches returned incompatible embedding spaces"
                )
            for index, prepared in enumerate(batch.inputs):
                value = by_index[index].value
                if not isinstance(value, tuple):
                    raise EmbeddingResultMismatchError(
                        "vector indexing requires float embeddings, not encoded values"
                    )
                if len(value) != config.embedding_dimensions:
                    raise EmbeddingResultMismatchError(
                        "embedding result dimension does not match indexing configuration"
                    )
                embedded.append(EmbeddedChunk(record=prepared.record, vector=value))

        return EmbeddingRun(
            chunks=tuple(embedded),
            configuration_fingerprint=config.embedding_configuration_fingerprint,
            dimension=config.embedding_dimensions,
            embedding_space=expected_space,
        )
