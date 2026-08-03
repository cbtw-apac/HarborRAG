from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest

from harborrag_core.ingestion import ActiveDocumentVersion
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_core.schemas.vector import SparseVector, VectorSearchResult
from harborrag_engine.retrieval import (
    ActiveVersionCandidateValidator,
    AuthoritativeProjectionSearch,
    AuthoritativeSearchRequest,
    RetrievalLane,
)


class ActiveVersions:
    def __init__(self, versions: Mapping[str, str]) -> None:
        self._versions = versions

    async def active_versions(
        self,
        document_ids: Sequence[str],
    ) -> Mapping[str, ActiveDocumentVersion]:
        return {
            document_id: ActiveDocumentVersion(
                document_id=document_id,
                document_version_id=version,
            )
            for document_id in document_ids
            if (version := self._versions.get(document_id)) is not None
        }


class SearchRepository:
    def __init__(
        self,
        routes: Sequence[VectorSearchResult],
        evidence: Sequence[VectorSearchResult] = (),
    ) -> None:
        self._routes = list(routes)
        self._evidence = list(evidence)
        self.dense_queries = []
        self.sparse_queries = []
        self.hybrid_queries = []

    async def search(self, query, *, context):
        self.dense_queries.append((query, context))
        return self._select(query.index_name, query.top_k)

    async def sparse_search(self, query, *, context):
        self.sparse_queries.append((query, context))
        return self._select(query.index_name, query.top_k)

    async def hybrid_search(self, query, *, context):
        self.hybrid_queries.append((query, context))
        return self._select(query.index_name, query.top_k)

    def _select(self, collection: str, limit: int) -> list[VectorSearchResult]:
        records = self._routes if "routes" in collection else self._evidence
        return records[:limit]


def _candidate(index: int, *, version: str) -> VectorSearchResult:
    return VectorSearchResult(
        id=f"point-{index}",
        score=0.9,
        raw_score=0.9,
        payload={
            "chunk_id": f"chunk-{index}",
            "document_id": f"document-{index}",
            "document_version_id": version,
        },
    )


@pytest.mark.asyncio
async def test_search_expands_past_stale_top_k_candidates() -> None:
    routes = [
        *(_candidate(index, version="stale") for index in range(25)),
        _candidate(25, version="active-25"),
    ]
    repository = SearchRepository(routes)
    search = AuthoritativeProjectionSearch(
        repository,
        ActiveVersionCandidateValidator(
            ActiveVersions(
                {
                    **{f"document-{index}": f"active-{index}" for index in range(25)},
                    "document-25": "active-25",
                }
            )
        ),
    )

    result = await search.search(
        AuthoritativeSearchRequest(
            lane=RetrievalLane.DENSE,
            top_k=1,
            dense_vector=(1.0, 0.0, 0.0),
        ),
        context=StorageOperationContext.system(tenant_id="tenant-1"),
    )

    assert [candidate.id for candidate in result.candidates] == ["point-25"]
    assert result.diagnostics.search_window == 40
    assert result.diagnostics.stale_count == 25
    assert [query.top_k for query, _ in repository.dense_queries] == [
        20,
        20,
        40,
        40,
    ]


@pytest.mark.asyncio
async def test_sparse_lane_uses_sparse_profile_without_payload_only_filters() -> None:
    candidate = _candidate(1, version="active-1")
    repository = SearchRepository([candidate])
    search = AuthoritativeProjectionSearch(
        repository,
        ActiveVersionCandidateValidator(ActiveVersions({"document-1": "active-1"})),
    )

    result = await search.search(
        AuthoritativeSearchRequest(
            lane=RetrievalLane.SPARSE,
            top_k=1,
            sparse_vector=SparseVector(indices=[7], values=[1.0]),
        ),
        context=StorageOperationContext.system(tenant_id="tenant-1"),
    )

    assert [item.id for item in result.candidates] == ["point-1"]
    assert not repository.dense_queries
    assert not repository.hybrid_queries
    assert len(repository.sparse_queries) == 2
    assert all(query.filters is None for query, _ in repository.sparse_queries)
