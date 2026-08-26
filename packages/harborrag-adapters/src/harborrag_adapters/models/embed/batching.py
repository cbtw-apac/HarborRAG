from __future__ import annotations

from dataclasses import dataclass, field

from harborrag_core.models.embed import HarborEmbedRequest, HarborEmbedResponse
from harborrag_core.models.errors import HarborEmbedError, HarborEmbedPartialBatchError

from .configs import HarborEmbedProviderConfig
from .normalization import merge_embedding_batches, normalize_embedding_batch


@dataclass
class EmbeddingBatchAccumulator:
    """Collect one deployment attempt without exposing partial vectors."""

    logical_model: str
    embedding_space: str
    deployment: HarborEmbedProviderConfig
    request: HarborEmbedRequest
    responses: list[HarborEmbedResponse] = field(default_factory=list)

    def add(self, raw: object, *, offset: int, size: int, latency_ms: float) -> None:
        """Normalize and retain one successful provider batch in input order."""

        self.responses.append(
            normalize_embedding_batch(
                raw,
                input_count=size,
                index_offset=offset,
                logical_model=self.logical_model,
                embedding_space=self.embedding_space,
                deployment=self.deployment,
                request_id=self.request_id,
                latency_ms=latency_ms,
                normalize_vectors=bool(self.request.normalize),
            )
        )

    @property
    def request_id(self) -> str:
        """Return the identity guaranteed by embedding request preparation."""

        request_id = self.request.metadata.request_id
        if request_id is None:
            raise RuntimeError("prepared embedding request has no request ID")
        return request_id

    def failure(self, error: HarborEmbedError, *, batch_index: int, completed: int) -> Exception:
        """Discard partial vectors and return a stable partial-batch error.

        Retryable partial failures deliberately propagate retryability: the policy
        layer retries the complete request from the first batch, trading the cost
        of re-embedding completed batches for availability. Partial vectors are
        never returned or resumed mid-request.
        """

        if completed == 0:
            return error
        return HarborEmbedPartialBatchError(
            "embedding batch failed after partial completion; no partial response was returned",
            operation="embed",
            provider=self.deployment.provider.value,
            logical_model=self.logical_model,
            provider_model=self.deployment.model,
            deployment=self.deployment.name,
            request_id=self.request_id,
            retryable=error.retryable,
            original_exception=error,
            metadata={
                "failed_batch_index": batch_index,
                "completed_inputs": completed,
                "total_inputs": len(self.request.inputs),
            },
        )

    def complete(self, latency_ms: float) -> HarborEmbedResponse:
        """Merge all batches into one provider-neutral response."""

        return merge_embedding_batches(
            self.responses,
            request_id=self.request_id,
            total_latency_ms=latency_ms,
            retry_count=0,
        )
