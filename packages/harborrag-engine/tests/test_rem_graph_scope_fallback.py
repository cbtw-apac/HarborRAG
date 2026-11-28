"""Graph reads past the document enumeration budget fall back to readable source scopes.

The fake store applies the access scope the way the FalkorDB access predicate does,
so a document of a readable source reaches the engine even when the reader is denied
it, and only the post-read permission check can keep it out.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import pytest

from harborrag_core.chunking import RelationType
from harborrag_core.contracts.errors import (
    HarborAuthorizationUnavailableError,
    HarborConflictError,
)
from harborrag_core.contracts.reader import GraphNodeResolveRequest
from harborrag_core.ingestion import (
    ActiveDocumentVersion,
    DocumentIdentityBuilder,
    GraphEdgeRecord,
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeGraphTraversal,
    KnowledgeNodeKind,
)
from harborrag_core.retrieval import (
    GraphAccessScope,
    GraphNodeResolutionQuery,
    GraphNodeResolutionResult,
    GraphNodeSelectorKind,
    GraphPath,
    GraphPathQuery,
    GraphPathResult,
    GraphSubgraphQuery,
    GraphTriplet,
    GraphTripletQuery,
    GraphTripletResult,
)
from harborrag_core.security import AccessContext
from harborrag_core.storage import StorageOperationContext
from harborrag_engine.retrieval import AuthoritativeGraphSearch
from harborrag_engine.retrieval.graph_visibility import apply_graph_permissions
from harborrag_engine.retrieval.service.reader_resources import ReaderResources
from harborrag_engine.retrieval.service.readers import ReaderRetrieval

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

TENANT = "tenant-1"
ACCESS = AccessContext(tenant_id=TENANT, principal_id="reader-1")
CONTEXT = StorageOperationContext.for_access(ACCESS)
READABLE_SOURCE = "scope-engineering"
HIDDEN_SOURCE = "scope-finance"
OPEN = "document-open"
DENIED = "document-denied"
HIDDEN = "document-hidden"
BUDGET = 10_000


class Authorizer:
    """Canonical permissions with the adapter's enumeration-budget semantics.

    ``limit`` is a budget, not a truncation: more readable rows than it allows
    raise ``HarborConflictError``. ``DENIED`` lives in the readable source but
    carries an explicit per-document deny, so it is never readable.
    """

    def __init__(self, readable_documents: Iterable[str]) -> None:
        self.documents = frozenset(readable_documents)
        self.sources = frozenset({READABLE_SOURCE})

    async def allowed_document_ids(self, tenant_id, *, access, limit=BUDGET):
        del tenant_id, access
        return _within_budget(self.documents, limit)

    async def allowed_source_scope_ids(self, tenant_id, *, access, limit=BUDGET):
        del tenant_id, access
        return _within_budget(self.sources, limit)

    async def authorized_document_ids(self, tenant_id, document_ids, *, access):
        del tenant_id, access
        return set(document_ids) & self.documents

    async def authorized_source_scope_ids(self, tenant_id, source_scope_ids, *, access):
        del tenant_id, access
        return set(source_scope_ids) & self.sources


def _within_budget(values: frozenset[str], limit: int) -> tuple[str, ...]:
    if len(values) > max(1, min(limit, BUDGET)):
        raise HarborConflictError("authorized document enumeration exceeds the configured budget")
    return tuple(sorted(values))


def _large_reader() -> Authorizer:
    return Authorizer({OPEN, *(f"document-{index:05d}" for index in range(BUDGET))})


def _small_reader() -> Authorizer:
    return Authorizer({OPEN})


IDENTITY = DocumentIdentityBuilder()


def _version_key(document_id: str) -> str:
    return IDENTITY.document_version_node_key(
        document_id=document_id, document_version_id="version-1"
    )


def _section_key(document_id: str) -> str:
    return IDENTITY.node_key(
        node_kind=KnowledgeNodeKind.STRUCTURE,
        entity_type=GraphEntityType.SECTION.value,
        logical_id=f"{document_id}#section",
        document_version_id="version-1",
    )


def _version_node(document_id: str, source: str) -> GraphNodeRecord:
    return GraphNodeRecord(
        node_key=_version_key(document_id),
        node_kind=KnowledgeNodeKind.DOCUMENT_VERSION,
        entity_type=GraphEntityType.DOCUMENT_VERSION,
        logical_id=document_id,
        ownership_scope=GraphOwnershipScope.DOCUMENT_VERSION,
        owner_id=TENANT,
        document_id=document_id,
        document_version_id="version-1",
        source_scope_id=source,
        title="Release guide",
    )


def _section_node(document_id: str, source: str) -> GraphNodeRecord:
    return _version_node(document_id, source).model_copy(
        update={
            "node_key": _section_key(document_id),
            "node_kind": KnowledgeNodeKind.STRUCTURE,
            "entity_type": GraphEntityType.SECTION,
            "logical_id": f"{document_id}#section",
        }
    )


def _contains(document_id: str, source: str) -> GraphEdgeRecord:
    return GraphEdgeRecord(
        relation_id=f"contains-{document_id}",
        relation_type=RelationType.CONTAINS,
        source_node_key=_version_key(document_id),
        target_node_key=_section_key(document_id),
        ownership_scope=GraphOwnershipScope.DOCUMENT_VERSION,
        owner_id=TENANT,
        source_scope_id=source,
        document_id=document_id,
        document_version_id="version-1",
        source_relation_version="v1",
        source_explicit=False,
    )


def _links_to(source: str, target: str) -> GraphEdgeRecord:
    return _contains(source, READABLE_SOURCE).model_copy(
        update={
            "relation_id": f"link-{source}-{target}",
            "relation_type": RelationType.LINKS_TO,
            "source_node_key": _version_key(source),
            "target_node_key": _version_key(target),
        }
    )


CORPUS = ((OPEN, READABLE_SOURCE), (DENIED, READABLE_SOURCE), (HIDDEN, HIDDEN_SOURCE))
TRIPLETS = tuple(
    GraphTriplet(
        subject=_version_node(document_id, source),
        predicate=_contains(document_id, source),
        object=_section_node(document_id, source),
    )
    for document_id, source in CORPUS
)
NODES = tuple(
    node
    for document_id, source in CORPUS
    for node in (_version_node(document_id, source), _section_node(document_id, source))
)
RELATIONS = (
    *(_contains(document_id, source) for document_id, source in CORPUS),
    _links_to(OPEN, DENIED),
)


def _admitted(record: GraphNodeRecord | GraphEdgeRecord, scope: GraphAccessScope | None) -> bool:
    """The FalkorDB ``access_predicate``, for the records this corpus holds."""

    if scope is None or scope.tenant_shared:
        return True
    if record.document_id is not None:
        by_source = scope.document_access == "by_source_scope"
        return str(record.document_id) in scope.document_ids or (
            by_source and record.source_scope_id in scope.source_scope_ids
        )
    return record.source_scope_id in scope.source_scope_ids


def _all_admitted(
    records: Sequence[GraphNodeRecord | GraphEdgeRecord], scope: GraphAccessScope | None
) -> bool:
    return all(_admitted(record, scope) for record in records)


class Store:
    """An in-memory graph store that filters by the access scope before it limits."""

    def __init__(self, *, truncated: bool = False) -> None:
        self.truncated = truncated
        self.scopes: list[GraphAccessScope | None] = []

    async def search_triplets(self, query, *, context):
        del context
        self.scopes.append(query.access_scope)
        triplets = tuple(
            item
            for item in TRIPLETS
            if _all_admitted((item.subject, item.predicate, item.object), query.access_scope)
        )
        return GraphTripletResult(triplets=triplets, truncated=self.truncated)

    async def find_paths(self, query, *, context):
        del context
        self.scopes.append(query.access_scope)
        path = GraphPath(
            nodes=(_version_node(OPEN, READABLE_SOURCE), _version_node(DENIED, READABLE_SOURCE)),
            relations=(_links_to(OPEN, DENIED),),
        )
        admitted = _all_admitted((*path.nodes, *path.relations), query.access_scope)
        return GraphPathResult(paths=(path,) if admitted else (), truncated=self.truncated)

    async def expand_subgraph(self, query, *, context):
        del context
        self.scopes.append(query.access_scope)
        return KnowledgeGraphTraversal(
            nodes=tuple(node for node in NODES if _admitted(node, query.access_scope)),
            relations=tuple(
                relation for relation in RELATIONS if _admitted(relation, query.access_scope)
            ),
            truncated=self.truncated,
        )

    async def resolve_nodes(self, query, *, context):
        del context
        self.scopes.append(query.access_scope)
        return GraphNodeResolutionResult(
            candidates=tuple(
                node
                for node in NODES
                if node.node_kind == KnowledgeNodeKind.DOCUMENT_VERSION
                and _admitted(node, query.access_scope)
            ),
            truncated=self.truncated,
        )


class ActiveVersions:
    async def active_versions(self, document_ids):
        return {
            document_id: ActiveDocumentVersion(
                document_id=document_id, document_version_id="version-1"
            )
            for document_id in document_ids
        }


def _search(store: Store, authorizer: Authorizer) -> AuthoritativeGraphSearch:
    return AuthoritativeGraphSearch(store, ActiveVersions(), authorizer)  # type: ignore[arg-type]


def _documents(records: Iterable[GraphNodeRecord | GraphEdgeRecord]) -> set[str]:
    return {str(record.document_id) for record in records if record.document_id is not None}


async def test_triplets_of_a_large_reader_never_include_a_denied_document() -> None:
    store = Store()

    result = await _search(store, _large_reader()).triplets(
        GraphTripletQuery(predicate=RelationType.CONTAINS), context=CONTEXT
    )

    scope = store.scopes[0]
    assert scope is not None
    assert scope.document_access == "by_source_scope"
    assert scope.document_ids == ()
    assert scope.source_scope_ids == (READABLE_SOURCE,)
    assert _documents(item.subject for item in result.triplets) == {OPEN}
    assert result.diagnostics.candidate_count == 1


async def test_paths_of_a_large_reader_never_cross_a_denied_document() -> None:
    result = await _search(Store(), _large_reader()).paths(
        GraphPathQuery(start_node=_version_key(OPEN), end_node=_version_key(DENIED)),
        context=CONTEXT,
    )

    assert result.paths == ()
    assert result.diagnostics.candidate_count == 0


async def test_subgraph_of_a_large_reader_never_includes_a_denied_document() -> None:
    result = await _search(Store(), _large_reader()).subgraph(
        GraphSubgraphQuery(start_node=_version_key(OPEN), max_nodes=20), context=CONTEXT
    )

    assert _documents(result.graph.nodes) == {OPEN}
    assert _documents(result.graph.relations) == {OPEN}
    assert [relation.relation_type for relation in result.graph.relations] == [
        RelationType.CONTAINS
    ]


def _reader(store: Store, authorizer: Authorizer) -> ReaderRetrieval:
    return ReaderRetrieval(
        ReaderResources(
            vectors=None,  # type: ignore[arg-type]
            validator=None,  # type: ignore[arg-type]
            permissions=None,  # type: ignore[arg-type]
            topology=authorizer,  # type: ignore[arg-type]
            snapshots=None,
            chunks=None,  # type: ignore[arg-type]
            sources=None,
            graph=store,  # type: ignore[arg-type]
        )
    )


def _resolution(kind: GraphNodeSelectorKind, value: str) -> GraphNodeResolveRequest:
    return GraphNodeResolveRequest(
        ACCESS, GraphNodeResolutionQuery(selector_kind=kind, value=value)
    )


async def test_node_resolution_of_a_large_reader_never_returns_a_denied_document() -> None:
    response = await _reader(Store(), _large_reader()).resolve_graph_nodes(
        _resolution(GraphNodeSelectorKind.NODE_KEY, _version_key(OPEN))
    )

    assert _documents(response.candidates) == {OPEN}
    assert response.truncated is False


async def test_store_truncation_from_hidden_rows_is_not_reported_past_the_budget() -> None:
    result = await _search(Store(truncated=True), _large_reader()).triplets(
        GraphTripletQuery(predicate=RelationType.CONTAINS), context=CONTEXT
    )

    assert result.diagnostics.projection_truncated is False


async def test_a_small_reader_keeps_the_listed_scope_and_its_results() -> None:
    store = Store(truncated=True)

    result = await _search(store, _small_reader()).triplets(
        GraphTripletQuery(predicate=RelationType.CONTAINS), context=CONTEXT
    )

    scope = store.scopes[0]
    assert scope is not None
    assert scope.document_access == "listed"
    assert scope.document_ids == (OPEN,)
    assert scope.source_scope_ids == (READABLE_SOURCE,)
    assert _documents(item.subject for item in result.triplets) == {OPEN}
    assert result.diagnostics.candidate_count == 1
    assert result.diagnostics.projection_truncated is True


async def test_tenant_nodes_stay_readable_for_a_large_reader() -> None:
    tenant_node = GraphNodeRecord(
        node_key="tenant-node",
        node_kind=KnowledgeNodeKind.TENANT,
        entity_type=GraphEntityType.TENANT,
        logical_id=TENANT,
        ownership_scope=GraphOwnershipScope.TENANT,
        owner_id=TENANT,
    )

    states = await apply_graph_permissions(
        {"tenant-node": tenant_node}, {"tenant-node": "active"}, _large_reader(), CONTEXT
    )

    assert states == {"tenant-node": "active"}


@pytest.mark.parametrize(
    "query",
    [
        GraphTripletQuery(subject="Release guide"),
        GraphTripletQuery(subject=_version_key(OPEN), object=f"{OPEN}#section"),
        GraphPathQuery(start_node=_version_key(OPEN), end_node=DENIED),
        GraphSubgraphQuery(start_node="Release guide"),
    ],
    ids=["triplet-title", "triplet-logical-id", "path-logical-id", "subgraph-title"],
)
async def test_a_large_reader_cannot_select_by_title_or_logical_id(
    query: GraphTripletQuery | GraphPathQuery | GraphSubgraphQuery,
) -> None:
    store = Store()
    search = _search(store, _large_reader())
    read = {
        GraphTripletQuery: search.triplets,
        GraphPathQuery: search.paths,
        GraphSubgraphQuery: search.subgraph,
    }[type(query)]

    with pytest.raises(HarborAuthorizationUnavailableError, match="^AUTHORIZATION_UNAVAILABLE$"):
        await read(query, context=CONTEXT)  # type: ignore[arg-type]

    assert store.scopes == []


@pytest.mark.parametrize(
    "kind", [GraphNodeSelectorKind.EXACT_TITLE, GraphNodeSelectorKind.PROVIDER_ID]
)
async def test_a_large_reader_cannot_resolve_nodes_by_title_or_provider_id(
    kind: GraphNodeSelectorKind,
) -> None:
    store = Store()

    with pytest.raises(HarborAuthorizationUnavailableError, match="^AUTHORIZATION_UNAVAILABLE$"):
        await _reader(store, _large_reader()).resolve_graph_nodes(_resolution(kind, OPEN))

    assert store.scopes == []


async def test_a_large_reader_still_selects_by_node_key() -> None:
    store = Store()

    result = await _search(store, _large_reader()).triplets(
        GraphTripletQuery(subject=_version_key(OPEN), object=_section_key(OPEN)),
        context=CONTEXT,
    )

    assert len(store.scopes) == 1
    assert _documents(item.subject for item in result.triplets) == {OPEN}


async def test_a_small_reader_still_selects_by_title_and_logical_id() -> None:
    store = Store()
    search = _search(store, _small_reader())

    triplets = await search.triplets(GraphTripletQuery(subject="Release guide"), context=CONTEXT)
    subgraph = await search.subgraph(GraphSubgraphQuery(start_node=OPEN), context=CONTEXT)
    nodes = await _reader(store, _small_reader()).resolve_graph_nodes(
        _resolution(GraphNodeSelectorKind.EXACT_TITLE, "Release guide")
    )

    assert [scope.document_access for scope in store.scopes if scope is not None] == [
        "listed",
        "listed",
        "listed",
    ]
    assert _documents(item.subject for item in triplets.triplets) == {OPEN}
    assert _documents(subgraph.graph.nodes) == {OPEN}
    assert _documents(nodes.candidates) == {OPEN}
