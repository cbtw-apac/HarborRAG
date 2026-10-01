"""Graph observation never walks the graph unscoped or from unpermitted seeds."""

from __future__ import annotations

import pytest
from retrieval_test_support import (
    TENANT_SHARED_READER,
    FakeActiveVersions,
    FakeGraphRepository,
    FakeVectorRepository,
)
from retrieval_test_support import (
    policy as _policy,
)
from retrieval_test_support import (
    resources as _resources,
)

from harborrag_core.ingestion import ActiveDocumentVersion
from harborrag_core.retrieval import GraphAccessScope
from harborrag_core.schemas.vector import VectorSearchResult
from harborrag_core.security import AccessContext
from harborrag_core.storage import StorageOperationContext
from harborrag_runtime.retrieval import RetrievalOptions, RuntimeRetrievalService
from harborrag_runtime.retrieval.graph_observation import GraphObservation, GraphObserver
from harborrag_runtime.retrieval.permissions import RetrievalPermissions


def _candidate(chunk_id: str, document_id: str) -> VectorSearchResult:
    return VectorSearchResult(
        id=f"point-{chunk_id}",
        score=0.9,
        raw_score=0.9,
        relevance=0.9,
        payload={
            "chunk_id": chunk_id,
            "document_id": document_id,
            "document_version_id": f"version-{document_id}",
            "record_kind": "evidence",
            "chunk_kind": "text",
            "connector_type": "local",
            "content": "The activity timeout is 30 seconds.",
        },
    )


def _context(corpus_mode: str = "source_acl") -> StorageOperationContext:
    return StorageOperationContext.for_access(
        AccessContext(principal_id="reader", tenant_id="tenant-1", corpus_mode=corpus_mode),
        operation_kind="retrieval",
    )


class _Authorizer:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error

    async def allowed_document_ids(self, tenant_id, *, access, limit=10000):
        del tenant_id, access, limit
        if self.error is not None:
            raise self.error
        return ("document-1",)

    async def allowed_source_scope_ids(self, tenant_id, *, access, limit=10000):
        del tenant_id, access, limit
        return ("scope-1",)

    async def authorized_document_ids(self, tenant_id, document_ids, *, access):
        del tenant_id, access
        return set(document_ids) & {"document-1"}

    async def authorized_source_scope_ids(self, tenant_id, source_scope_ids, *, access):
        del tenant_id, access
        return set(source_scope_ids) & {"scope-1"}


@pytest.mark.asyncio
async def test_observer_scopes_every_walk_with_the_authorizer_allowlists() -> None:
    graph = FakeGraphRepository()

    await GraphObserver(graph, topology=_Authorizer()).observe(
        (_candidate("chunk-1", "document-1"),),
        context=_context(),
        request_id="request-1",
        memory_seeds=("node-atlas",),
    )

    expected = GraphAccessScope(document_ids=("document-1",), source_scope_ids=("scope-1",))
    assert [key for key, _ in graph.queries] == ["chunk-1", "node-atlas"]
    assert [kwargs["access_scope"] for _, kwargs in graph.queries] == [expected, expected]


@pytest.mark.asyncio
async def test_observer_without_an_authorizer_skips_a_source_acl_reader() -> None:
    graph = FakeGraphRepository()

    observation = await GraphObserver(graph).observe(
        (_candidate("chunk-1", "document-1"),),
        context=_context("source_acl"),
        request_id="request-1",
    )

    assert observation == GraphObservation()
    assert graph.queries == []


@pytest.mark.asyncio
async def test_observer_without_an_authorizer_scopes_a_tenant_shared_reader() -> None:
    graph = FakeGraphRepository()

    await GraphObserver(graph).observe(
        (_candidate("chunk-1", "document-1"),),
        context=_context("tenant_shared"),
        request_id="request-1",
    )

    assert [kwargs["access_scope"] for _, kwargs in graph.queries] == [
        GraphAccessScope(tenant_shared=True)
    ]


@pytest.mark.asyncio
async def test_observer_fails_closed_when_the_authorizer_is_unavailable() -> None:
    graph = FakeGraphRepository()

    observation = await GraphObserver(
        graph, topology=_Authorizer(error=ConnectionError("authority unavailable"))
    ).observe(
        (_candidate("chunk-1", "document-1"),),
        context=_context(),
        request_id="request-1",
    )

    assert observation == GraphObservation()
    assert graph.queries == []


class _TwoDocumentVectors(FakeVectorRepository):
    def _results(self, collection: str) -> list[VectorSearchResult]:
        if "evidence" not in collection:
            return []
        return [_candidate("chunk-1", "document-1"), _candidate("chunk-2", "document-2")]


class _TwoActiveDocuments(FakeActiveVersions):
    async def active_versions(self, document_ids):
        return {
            document_id: ActiveDocumentVersion(
                document_id=document_id,
                document_version_id=f"version-{document_id}",
            )
            for document_id in document_ids
        }


class _DropDocumentTwo(RetrievalPermissions):
    def __init__(self) -> None:
        super().__init__(None)

    async def validate(self, candidates, context):
        del context
        return tuple(item for item in candidates if item.payload["document_id"] != "document-2")


@pytest.mark.asyncio
async def test_service_seeds_the_observer_only_with_permitted_candidates() -> None:
    graph = FakeGraphRepository()
    service = RuntimeRetrievalService(
        resources=_resources(
            graph=graph,
            vectors=_TwoDocumentVectors(),
            active_versions=_TwoActiveDocuments(),
        ),
        policy=_policy(),
    )
    service._permissions = _DropDocumentTwo()

    await service.retrieve(
        "release",
        tenant_id="tenant-1",
        options=RetrievalOptions(observe_graph=True),
        access=TENANT_SHARED_READER,
    )

    seeds = [key for key, _ in graph.queries]
    assert "chunk-1" in seeds
    assert "chunk-2" not in seeds
