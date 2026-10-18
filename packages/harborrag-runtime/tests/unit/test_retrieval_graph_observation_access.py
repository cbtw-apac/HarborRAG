"""Graph observation walks only what the caller may read."""

from __future__ import annotations

import pytest
from retrieval_test_support import FakeGraphRepository
from retrieval_test_support import policy as _policy
from retrieval_test_support import resources as _resources

from harborrag_core.chunking import RelationType
from harborrag_core.indexing import VectorSearchResult
from harborrag_core.ingestion import (
    GraphEdgeRecord,
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeGraphTraversal,
    KnowledgeNodeKind,
)
from harborrag_core.retrieval import GraphAccessScope
from harborrag_core.schemas.ids import TenantId
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


# --- a reader narrowed to some datasources on a shared corpus ---------------------------

ENG, HR = "scope-eng", "scope-hr"
DOCUMENT_SCOPES = {"doc-eng": ENG, "doc-eng-2": ENG, "doc-hr": HR}
_ENTITY_TYPES = {
    KnowledgeNodeKind.CHUNK: GraphEntityType.CHUNK,
    KnowledgeNodeKind.STRUCTURE: GraphEntityType.SECTION,
    KnowledgeNodeKind.DOCUMENT_VERSION: GraphEntityType.DOCUMENT_VERSION,
}


def _node(
    key: str, kind: KnowledgeNodeKind, document: str, title: str | None = None
) -> GraphNodeRecord:
    return GraphNodeRecord(
        node_key=key,
        node_kind=kind,
        entity_type=_ENTITY_TYPES[kind],
        logical_id=key,
        ownership_scope=GraphOwnershipScope.DOCUMENT_VERSION,
        owner_id="acme",
        source_scope_id=DOCUMENT_SCOPES[document],
        document_id=document,
        document_version_id=f"version-{document}",
        title=title,
    )


def _relation(
    relation_id: str, relation_type: RelationType, source: str, target: str, document: str
) -> GraphEdgeRecord:
    return GraphEdgeRecord(
        relation_id=relation_id,
        relation_type=relation_type,
        source_node_key=source,
        target_node_key=target,
        ownership_scope=GraphOwnershipScope.DOCUMENT_VERSION,
        owner_id="acme",
        source_scope_id=DOCUMENT_SCOPES[document],
        document_id=document,
        document_version_id=f"version-{document}",
        source_relation_version="graph-v1",
        source_explicit=False,
    )


ENG_CHUNK = _node("chunk-eng", KnowledgeNodeKind.CHUNK, "doc-eng")
ENG_SECTION = _node("section-eng", KnowledgeNodeKind.STRUCTURE, "doc-eng", "Rollout")
ENG_DOCUMENT = _node(
    "dv-eng", KnowledgeNodeKind.DOCUMENT_VERSION, "doc-eng", "Eng: release runbook"
)
ENG_ONBOARDING = _node(
    "dv-eng-2", KnowledgeNodeKind.DOCUMENT_VERSION, "doc-eng-2", "Eng: onboarding"
)
HR_CHUNK = _node("chunk-hr", KnowledgeNodeKind.CHUNK, "doc-hr")
HR_DOCUMENT = _node(
    "dv-hr", KnowledgeNodeKind.DOCUMENT_VERSION, "doc-hr", "HR: 2027 reorganisation plan"
)
HR_TOKENS = ("doc-hr", "dv-hr", "chunk-hr", "HR: 2027", "scope-hr", "doc-eng-2", "onboarding")
ENG_CANDIDATE = VectorSearchResult(
    id="point-eng", score=1.0, raw_score=1.0, payload={"chunk_id": "chunk-eng"}
)


class SharedCorpusGraph(FakeGraphRepository):
    """A graph walked as FalkorDB walks a shared corpus: across every datasource.

    The engineering result reaches its own document, and over a cross-document link the
    HR plan. The memory seed is an HR chunk, and its walk reaches an engineering document
    the result's walk does not.
    """

    async def traverse(self, start_node_key, **kwargs):
        self.queries.append((start_node_key, kwargs))
        if start_node_key == "chunk-eng":
            return KnowledgeGraphTraversal(
                nodes=(ENG_CHUNK, ENG_SECTION, ENG_DOCUMENT, HR_DOCUMENT),
                relations=(
                    _relation(
                        "supports-eng", RelationType.SUPPORTS, "chunk-eng", "section-eng", "doc-eng"
                    ),
                    _relation(
                        "contains-eng", RelationType.CONTAINS, "dv-eng", "section-eng", "doc-eng"
                    ),
                    # Owned by the engineering document, so only its HR end is hidden.
                    _relation("link-hr", RelationType.LINKS_TO, "section-eng", "dv-hr", "doc-eng"),
                ),
            )
        return KnowledgeGraphTraversal(
            nodes=(HR_CHUNK, HR_DOCUMENT, ENG_ONBOARDING),
            relations=(
                _relation("contains-hr", RelationType.CONTAINS, "dv-hr", "chunk-hr", "doc-hr"),
                _relation("link-eng", RelationType.LINKS_TO, "chunk-hr", "dv-eng-2", "doc-hr"),
            ),
            truncated=True,
        )


class SharedCorpusAuthority:
    """Canonical authority on a shared corpus: every published document, by entitlement."""

    def __init__(self) -> None:
        self.checks: list[tuple[str, ...]] = []

    @staticmethod
    def _entitled(access: AccessContext) -> set[str]:
        entitled = access.entitled_source_scopes
        return {scope for scope in (ENG, HR) if entitled is None or scope in entitled}

    async def allowed_document_ids(self, tenant_id, *, access, limit=10000):
        raise AssertionError("a shared corpus is walked without an allowlist")

    async def allowed_source_scope_ids(self, tenant_id, *, access, limit=10000):
        raise AssertionError("a shared corpus is walked without an allowlist")

    async def authorized_document_ids(self, tenant_id, document_ids, *, access):
        self.checks.append(tuple(document_ids))
        scopes = self._entitled(access)
        return {document for document in document_ids if DOCUMENT_SCOPES[document] in scopes}

    async def authorized_source_scope_ids(self, tenant_id, source_scope_ids, *, access):
        self.checks.append(tuple(source_scope_ids))
        return set(source_scope_ids) & self._entitled(access)


def _shared(entitled: frozenset[str] | None) -> StorageOperationContext:
    access = AccessContext(
        principal_id="user-alice",
        tenant_id=TenantId("acme"),
        corpus_mode="tenant_shared",
        entitled_source_scopes=entitled,
    )
    return StorageOperationContext.for_access(
        access, operation_kind="retrieval", idempotency_key="request-1"
    )


@pytest.mark.asyncio
async def test_a_narrowed_reader_observes_only_its_datasources_on_a_shared_corpus() -> None:
    graph = SharedCorpusGraph()

    observation = await GraphObserver(graph, SharedCorpusAuthority()).observe(  # type: ignore[arg-type]
        (ENG_CANDIDATE,),
        context=_shared(frozenset({ENG})),
        request_id="request-1",
        memory_seeds=("chunk-hr",),
    )

    # Both seeds were walked, unrestricted: the filter runs on what came back.
    assert [start for start, _ in graph.queries] == ["chunk-eng", "chunk-hr"]
    for token in HR_TOKENS:
        assert token not in repr(observation)
    # observe() degrades every failure to an empty observation, so what the reader may
    # see must still be there.
    [document] = observation.documents
    assert (document.document_id, document.title, document.sections) == (
        "doc-eng",
        "Eng: release runbook",
        ("Rollout",),
    )
    [neighborhood] = document.related_results
    assert {node["node_key"] for node in neighborhood.nodes} == {
        "chunk-eng",
        "section-eng",
        "dv-eng",
    }
    assert {relation["relation_id"] for relation in neighborhood.relations} == {
        "supports-eng",
        "contains-eng",
    }
    assert (observation.nodes, observation.relations, observation.truncated) == (3, 2, False)


@pytest.mark.asyncio
async def test_without_an_entitlement_set_the_observation_is_unchanged() -> None:
    authority = SharedCorpusAuthority()

    def observe(observer: GraphObserver):
        return observer.observe(
            (ENG_CANDIDATE,),
            context=_shared(None),
            request_id="request-1",
            memory_seeds=("chunk-hr",),
        )

    unfiltered = await observe(GraphObserver(SharedCorpusGraph()))  # type: ignore[arg-type]
    observed = await observe(GraphObserver(SharedCorpusGraph(), authority))  # type: ignore[arg-type]

    assert observed == unfiltered
    assert (observed.nodes, observed.relations, observed.truncated) == (6, 5, True)
    assert {document.document_id for document in observed.documents} == {
        "doc-eng",
        "doc-hr",
        "doc-eng-2",
    }
    assert authority.checks == []
