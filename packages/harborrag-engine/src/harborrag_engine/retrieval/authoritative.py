from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from harborrag_core.indexing import (
    HybridSearchQuery,
    SparseSearchQuery,
    SparseVector,
    VectorFilter,
    VectorSearchQuery,
    VectorSearchResult,
)
from harborrag_core.invariants import HarborInvariantError
from harborrag_core.storage import StorageOperationContext
from harborrag_engine.ingestion.projections.vector import EVIDENCE_INDEX

from .active_versions import ActiveVersionCandidateValidator

_INITIAL_OVERSAMPLE = 3
_MINIMUM_WINDOW = 20
_MAXIMUM_WINDOW = 1_000


class RetrievalLane(StrEnum):
    """Select the retrieval representation used to rank candidates."""

    DENSE = "dense"
    SPARSE = "sparse"
    HYBRID = "hybrid"


class ProjectionSearchRepository(Protocol):
    """Search version-addressed vector projections."""

    async def search(
        self,
        query: VectorSearchQuery,
        *,
        context: StorageOperationContext,
    ) -> list[VectorSearchResult]: ...

    async def sparse_search(
        self,
        query: SparseSearchQuery,
        *,
        context: StorageOperationContext,
    ) -> list[VectorSearchResult]: ...

    async def hybrid_search(
        self,
        query: HybridSearchQuery,
        *,
        context: StorageOperationContext,
    ) -> list[VectorSearchResult]: ...


@dataclass(frozen=True, slots=True)
class AuthoritativeSearchRequest:
    lane: RetrievalLane
    top_k: int
    dense_vector: tuple[float, ...] | None = None
    sparse_vector: SparseVector | None = None
    filters: VectorFilter | None = None
    dense_weight: float = 0.7

    def __post_init__(self) -> None:
        if not 1 <= self.top_k <= 100:
            raise ValueError("authoritative search top_k must be between 1 and 100")
        if self.lane in {RetrievalLane.DENSE, RetrievalLane.HYBRID}:
            if not self.dense_vector:
                raise ValueError("dense and hybrid retrieval require a dense vector")
        if self.lane in {RetrievalLane.SPARSE, RetrievalLane.HYBRID}:
            if self.sparse_vector is None:
                raise ValueError("sparse and hybrid retrieval require a sparse vector")
        if not 0 <= self.dense_weight <= 1:
            raise ValueError("dense retrieval weight must be between zero and one")


@dataclass(frozen=True, slots=True)
class AuthoritativeSearchDiagnostics:
    candidate_count: int
    accepted_count: int
    stale_count: int
    unpublished_count: int
    malformed_count: int
    search_window: int
    exhausted: bool


@dataclass(frozen=True, slots=True)
class AuthoritativeSearchResult:
    candidates: tuple[VectorSearchResult, ...]
    diagnostics: AuthoritativeSearchDiagnostics


class AuthoritativeProjectionSearch:
    """Over-fetch vector candidates and keep only Postgres-active document versions.

    This validates version activeness, not access. There is no intra-tenant permission
    model: isolation is physical, one vector collection per tenant.
    """

    def __init__(
        self,
        repository: ProjectionSearchRepository,
        validator: ActiveVersionCandidateValidator,
    ) -> None:
        self._repository = repository
        self._validator = validator

    async def search(
        self,
        request: AuthoritativeSearchRequest,
        *,
        context: StorageOperationContext,
    ) -> AuthoritativeSearchResult:
        window = min(
            _MAXIMUM_WINDOW,
            max(_MINIMUM_WINDOW, request.top_k * _INITIAL_OVERSAMPLE),
        )
        while True:
            evidence = await self._search_collection(
                EVIDENCE_INDEX,
                request,
                top_k=window,
                context=context,
            )
            candidates = tuple(evidence)
            validated = await self._validator.validate(candidates)
            accepted = validated.accepted[: request.top_k]
            exhausted = len(evidence) < window
            if len(accepted) >= request.top_k or exhausted or window == _MAXIMUM_WINDOW:
                return AuthoritativeSearchResult(
                    candidates=accepted,
                    diagnostics=AuthoritativeSearchDiagnostics(
                        candidate_count=len(candidates),
                        accepted_count=len(validated.accepted),
                        stale_count=validated.stale_count,
                        unpublished_count=validated.unpublished_count,
                        malformed_count=validated.malformed_count,
                        search_window=window,
                        exhausted=exhausted,
                    ),
                )
            window = min(_MAXIMUM_WINDOW, window * 2)

    async def _search_collection(
        self,
        collection: str,
        request: AuthoritativeSearchRequest,
        *,
        top_k: int,
        context: StorageOperationContext,
    ) -> list[VectorSearchResult]:
        if request.lane == RetrievalLane.DENSE:
            if request.dense_vector is None:
                raise HarborInvariantError("request.dense_vector must not be None here")
            return await self._repository.search(
                VectorSearchQuery(
                    index_name=collection,
                    vector=list(request.dense_vector),
                    top_k=top_k,
                    filters=request.filters,
                ),
                context=context,
            )
        if request.lane == RetrievalLane.SPARSE:
            if request.sparse_vector is None:
                raise HarborInvariantError("request.sparse_vector must not be None here")
            return await self._sparse_collection(
                collection,
                request.sparse_vector,
                filters=request.filters,
                top_k=top_k,
                context=context,
            )
        if request.dense_vector is None:
            raise HarborInvariantError("request.dense_vector must not be None here")
        if request.sparse_vector is None:
            raise HarborInvariantError("request.sparse_vector must not be None here")
        return await self._repository.hybrid_search(
            HybridSearchQuery(
                index_name=collection,
                vector=list(request.dense_vector),
                sparse_vector=request.sparse_vector,
                dense_weight=request.dense_weight,
                top_k=top_k,
                filters=request.filters,
            ),
            context=context,
        )

    async def _sparse_collection(
        self,
        collection: str,
        vector: SparseVector,
        *,
        filters: VectorFilter | None,
        top_k: int,
        context: StorageOperationContext,
    ) -> list[VectorSearchResult]:
        return await self._repository.sparse_search(
            SparseSearchQuery(
                index_name=collection,
                sparse_vector=vector,
                top_k=top_k,
                filters=filters,
            ),
            context=context,
        )
