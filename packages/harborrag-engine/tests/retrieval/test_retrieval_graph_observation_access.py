"""Graph observation walks only the part of the graph the caller may read."""

from __future__ import annotations

import pytest

from harborrag_core.indexing import VectorSearchResult
from harborrag_core.retrieval import GraphAccessScope
from harborrag_core.storage import StorageOperationContext
from harborrag_engine.retrieval.service.graph_observation import GraphObserver
from harborrag_engine.testing.retrieval_fakes import FakeGraphRepository

pytestmark = pytest.mark.unit


class _Authorizer:
    async def allowed_document_ids(self, tenant_id, *, access, limit=10_000):
        del tenant_id, access, limit
        return ("document-1",)

    async def allowed_source_scope_ids(self, tenant_id, *, access, limit=10_000):
        del tenant_id, access, limit
        return ("scope-1",)


@pytest.mark.asyncio
async def test_observation_walks_within_the_callers_access_scope() -> None:
    graph = FakeGraphRepository()
    observer = GraphObserver(graph, authorizer=_Authorizer())

    await observer.observe(
        [
            VectorSearchResult(
                id="point-1", score=0.9, raw_score=0.9, payload={"chunk_id": "chunk-1"}
            )
        ],
        context=StorageOperationContext.system("tenant-1"),
        request_id="request-1",
    )

    [(start_node_key, query)] = graph.queries
    assert start_node_key == "chunk-1"
    assert query.access_scope == GraphAccessScope(
        document_ids=("document-1",), source_scope_ids=("scope-1",)
    )
