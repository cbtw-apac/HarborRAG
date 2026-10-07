"""Graph observation walks only what the caller may read."""

from __future__ import annotations

import pytest
from retrieval_test_support import FakeGraphRepository
from retrieval_test_support import policy as _policy
from retrieval_test_support import resources as _resources

from harborrag_core.indexing import VectorSearchResult
from harborrag_core.retrieval import GraphAccessScope
from harborrag_core.security import AccessContext
from harborrag_core.storage import StorageOperationContext
from harborrag_runtime.retrieval import RetrievalOptions, RuntimeRetrievalService
from harborrag_runtime.retrieval.graph_observation import GraphObserver

CONTEXT = StorageOperationContext.system("tenant-1")
CANDIDATE = VectorSearchResult(
    id="point-1", score=1.0, raw_score=1.0, payload={"chunk_id": "chunk-1"}
)


class Grants:
    """A ``GraphVisibilityAuthorizer`` holding fixed allowlists."""

    def __init__(self, documents: tuple[str, ...] = (), sources: tuple[str, ...] = ()) -> None:
        self.documents = documents
        self.sources = sources

    async def allowed_document_ids(
        self, tenant_id: str, *, access: AccessContext, limit: int = 10000
    ) -> tuple[str, ...]:
        return self.documents

    async def allowed_source_scope_ids(
        self, tenant_id: str, *, access: AccessContext, limit: int = 10000
    ) -> tuple[str, ...]:
        return self.sources

    async def authorized_document_ids(
        self, tenant_id: str, document_ids: tuple[str, ...], *, access: AccessContext
    ) -> set[str]:
        return set(document_ids) & set(self.documents)

    async def authorized_source_scope_ids(
        self, tenant_id: str, source_scope_ids: tuple[str, ...], *, access: AccessContext
    ) -> set[str]:
        return set(source_scope_ids) & set(self.sources)


@pytest.mark.asyncio
async def test_every_walk_carries_the_callers_graph_scope() -> None:
    graph = FakeGraphRepository()

    await GraphObserver(graph, Grants(documents=("document-1",))).observe(  # type: ignore[arg-type]
        (CANDIDATE,), context=CONTEXT, request_id="request-1"
    )

    [(start, arguments)] = graph.queries
    assert start == "chunk-1"
    assert arguments["access_scope"] == GraphAccessScope(document_ids=("document-1",))


@pytest.mark.asyncio
async def test_a_reader_with_no_grants_is_never_walked() -> None:
    graph = FakeGraphRepository()

    observation = await GraphObserver(graph, Grants()).observe(  # type: ignore[arg-type]
        (CANDIDATE,), context=CONTEXT, request_id="request-1"
    )

    assert graph.queries == []
    assert observation.nodes == 0


@pytest.mark.asyncio
async def test_without_canonical_authority_the_walk_is_explicitly_unrestricted() -> None:
    graph = FakeGraphRepository()
    service = RuntimeRetrievalService(resources=_resources(graph=graph), policy=_policy())

    await service.retrieve(
        "release", tenant_id="tenant-1", options=RetrievalOptions(observe_graph=True)
    )

    [(_, arguments)] = graph.queries
    assert arguments["access_scope"] == GraphAccessScope.allow_all()
