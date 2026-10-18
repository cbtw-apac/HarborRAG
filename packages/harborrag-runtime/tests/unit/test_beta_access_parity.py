"""The API and MCP doors, and every reader tool, hold to the same access policy.

The doors build an :class:`AccessContext` differently, and the reader tools reach the corpus
only through the facades. Whatever route a call takes, the facades' hold must turn it into
the same context, and a caller the access file does not list must never reach the data.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_reader_access_policy import _ALICE, _SOURCE_ACL, _Config, _Scopes

from harborrag_core.chunking import RelationType
from harborrag_core.contracts.errors import HarborAuthError
from harborrag_core.contracts.reader import (
    DocumentContextChunk,
    DocumentContextResponse,
    DocumentListResponse,
    DocumentMetadata,
    EntityFindResponse,
    EntityMatch,
    EvidenceReadItem,
    EvidenceReadResponse,
    GraphNodeResolveResponse,
    SourceListResponse,
)
from harborrag_core.contracts.tools import ToolInvocationContext
from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.ingestion import (
    GraphEdgeRecord,
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeGraphTraversal,
    KnowledgeNodeKind,
    ReadableSource,
)
from harborrag_core.retrieval import GraphPath, GraphTriplet
from harborrag_core.schemas.ids import TenantId
from harborrag_core.security import AccessContext
from harborrag_core.topology.search import EvidenceBundle
from harborrag_engine.retrieval import RetrievalLane
from harborrag_engine.retrieval.graph import GraphSearchDiagnostics
from harborrag_engine.security.access_service import CorpusPolicy
from harborrag_engine.tools.catalog import build_reader_tool_catalog
from harborrag_engine.tools.dispatcher import ToolInvoker
from harborrag_engine.tools.references import KnowledgeReferenceStore
from harborrag_runtime.config.access_file import AccessFileWatcher
from harborrag_runtime.retrieval.access_policy import ReaderAccessPolicy
from harborrag_runtime.retrieval.contracts import RetrievalDiagnostics
from harborrag_runtime.retrieval.facades import GraphFacade, KnowledgeFacade, RetrievalFacade

REFUSAL = "not entitled to any datasource in this tenant"


@pytest.fixture
def config(tmp_path: Path) -> _Config:
    return _Config(tmp_path)


def _context(principal: str, tenant: str = "acme") -> AccessContext:
    return AccessContext(principal_id=principal, tenant_id=TenantId(tenant))


# --- the API door and the MCP door build their contexts differently -----------------------------


def _as_mcp_caller(monkeypatch: pytest.MonkeyPatch, subject: str, *tenants: str) -> None:
    """Authenticate as FastMCP does, so the real ``_request_context`` reads this token."""

    dependencies = pytest.importorskip("fastmcp.server.dependencies")
    token = SimpleNamespace(
        claims={"sub": subject, "role": "reader", "tenants": list(tenants)}, client_id="client"
    )
    monkeypatch.setattr(dependencies, "get_access_token", lambda: token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("shared_tenant", "mode"),
    [("acme", "tenant_shared"), ("globex", "source_acl")],
    ids=["shared-tenant", "other-tenant"],
)
async def test_api_and_mcp_contexts_hold_to_the_same_context(
    config: _Config, monkeypatch: pytest.MonkeyPatch, shared_tenant: str, mode: str
) -> None:
    """Chat, retrieval and the graph routes on the API, and the MCP tools, end up identical.

    The API door builds a bare context, as ``workflow_control/retrieval/query.py`` does. The MCP
    door is the real ``harborrag_mcp_server.server._request_context``, called with a FastMCP
    token (patched in as the mcp-server tests do) and the real access file watcher, so it also
    sets ``corpus_mode`` itself. The two contexts differ going in; the policy's hold is what
    makes them one.
    """

    from harborrag_mcp_server.server import _request_context

    config.write([_ALICE])
    _as_mcp_caller(monkeypatch, "alice-sub", "acme")
    watcher = AccessFileWatcher(config.access, catalog_path=config.catalog)
    policy = config.policy(
        corpus=CorpusPolicy(mode="tenant_shared", shared_tenant_id=shared_tenant)
    )

    api = _context("alice-sub")
    mcp = _request_context("acme", "tenant_shared", shared_tenant, watcher).access

    assert api.corpus_mode == "source_acl"
    assert mcp.corpus_mode == mode
    held_api = await policy.hold(api)
    held_mcp = await policy.hold(mcp)

    assert held_api.corpus_mode == held_mcp.corpus_mode == mode
    assert held_api.entitled_source_scopes == held_mcp.entitled_source_scopes
    assert held_api.entitled_source_scopes == frozenset(
        {"scope-conf", "scope-jira", "scope-wiki-a", "scope-wiki-b"}
    )
    assert held_api == held_mcp


# --- a stub service that would serve data, behind the real facades and the real policy ----------


def _node(key: str) -> GraphNodeRecord:
    return GraphNodeRecord(
        node_key=key,
        node_kind=KnowledgeNodeKind.SOURCE_ENTITY,
        entity_type=GraphEntityType.GITHUB_REPOSITORY,
        logical_id=f"provider-{key}",
        ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
        owner_id="acme",
        source_scope_id="scope-conf",
        title="Release runbook",
    )


def _edge(source: str, target: str) -> GraphEdgeRecord:
    return GraphEdgeRecord(
        relation_id=f"{source}->{target}",
        relation_type=RelationType.LINKS_TO,
        source_node_key=source,
        target_node_key=target,
        ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
        owner_id="acme",
        source_scope_id="scope-conf",
        source_relation_version="v1",
        source_explicit=True,
    )


_GRAPH_DIAGNOSTICS = GraphSearchDiagnostics(1, 1, 0, 0, False)


class _DataService:
    """Stands in for ``RuntimeRetrievalService``: real hold in front, real-looking data behind.

    ``hold_access`` runs the deployment's real ``ReaderAccessPolicy`` and records what it
    decided. Every other method is a data read: it records that it was reached, and returns
    a response with something in it, shaped so the tools accept it.
    """

    def __init__(self, policy: ReaderAccessPolicy) -> None:
        self._policy = policy
        self.holds: list[AccessContext | HarborAuthError] = []
        self.reached: list[str] = []

    async def hold_access(self, access: AccessContext) -> AccessContext:
        try:
            held = await self._policy.hold(access)
        except HarborAuthError as refusal:
            self.holds.append(refusal)
            raise
        self.holds.append(held)
        return held

    async def retrieve(self, *args: Any, **kwargs: Any) -> Any:
        self.reached.append("retrieve")
        result = RetrievalResult(
            "chunk-1",
            "Release acceptance runbook",
            0.9,
            {
                "document_id": "document-1",
                "document_version_id": "version-1",
                "chunk_kind": "text",
                "connector_type": "local",
                "citation_locator": {"start_line": 1},
                "document_title": "Runbook",
                "section_path": ["Overview"],
            },
        )
        diagnostics = RetrievalDiagnostics(1, 0, 0, 0, 5, 0, 0, False, 1.0)
        return SimpleNamespace(
            request_id="request-1",
            lane=RetrievalLane.HYBRID,
            results=(result,),
            diagnostics=diagnostics,
            evidence=EvidenceBundle(),
        )

    async def find_entities(self, request: Any) -> EntityFindResponse:
        self.reached.append("find_entities")
        match = EntityMatch(
            "node-1",
            "scope-conf",
            1,
            0.9,
            "Release runbook",
            "complete",
            ({"name": "owner", "values": ["alice"]},),
            ("chunk-1",),
        )
        return EntityFindResponse("request-1", (match,))

    async def read_evidence(self, request: Any) -> EvidenceReadResponse:
        self.reached.append("read_evidence")
        item = EvidenceReadItem(
            "chunk-1",
            "available",
            text="Release acceptance runbook",
            document_id="document-1",
            document_version_id="version-1",
            source_scope_id="scope-conf",
            connector_type="local",
            chunk_kind="text",
            ordinal=0,
            section_path=("Overview",),
            citation_locator={"start_line": 1},
        )
        return EvidenceReadResponse("request-1", (item,))

    async def get_document_context(self, request: Any) -> DocumentContextResponse:
        self.reached.append("get_document_context")
        chunk = DocumentContextChunk(
            "chunk-1", 0, "Release acceptance runbook", "text", ("Overview",), {"start_line": 1}
        )
        return DocumentContextResponse(
            "request-1", "ok", "document-1", "version-1", (chunk,), document_title="Runbook"
        )

    async def list_readable_sources(self, request: Any) -> SourceListResponse:
        self.reached.append("list_readable_sources")
        source = ReadableSource(
            source_scope_id="scope-conf",
            connector_type="local",
            display_name="Engineering wiki",
            ingestion_state="COMPLETED",
            last_source_check_at=None,
            active_document_count=3,
            entity_facets=(),
            entity_summaries="disabled",
        )
        return SourceListResponse("request-1", (source,))

    async def list_documents(self, request: Any) -> DocumentListResponse:
        self.reached.append("list_documents")
        document = DocumentMetadata("document-1", "version-1", "Runbook", "scope-conf", "local", 4)
        return DocumentListResponse("request-1", (document,))

    async def resolve_graph_nodes(self, request: Any) -> GraphNodeResolveResponse:
        self.reached.append("resolve_graph_nodes")
        return GraphNodeResolveResponse("request-1", (_node("node-1"),))

    async def search_graph_triplets(self, *args: Any, **kwargs: Any) -> Any:
        self.reached.append("search_graph_triplets")
        triplet = GraphTriplet(subject=_node("a"), predicate=_edge("a", "b"), object=_node("b"))
        return SimpleNamespace(triplets=(triplet,), diagnostics=_GRAPH_DIAGNOSTICS)

    async def search_graph_paths(self, *args: Any, **kwargs: Any) -> Any:
        self.reached.append("search_graph_paths")
        path = GraphPath(nodes=(_node("a"), _node("b")), relations=(_edge("a", "b"),))
        return SimpleNamespace(paths=(path,), diagnostics=_GRAPH_DIAGNOSTICS)

    async def search_graph_subgraph(self, *args: Any, **kwargs: Any) -> Any:
        self.reached.append("search_graph_subgraph")
        graph = KnowledgeGraphTraversal(
            nodes=(_node("a"), _node("b")), relations=(_edge("a", "b"),)
        )
        return SimpleNamespace(graph=graph, diagnostics=_GRAPH_DIAGNOSTICS)


@dataclass
class _Rig:
    """The 11 reader tools, built the way every consumer builds them, over ``service``."""

    invoker: ToolInvoker
    service: _DataService

    async def call(self, name: str, principal: str, tenant: str = "acme") -> dict[str, object]:
        arguments = {**_TOOL_CALLS[name]}
        if "tenant_id" in arguments:
            arguments["tenant_id"] = tenant
        context = ToolInvocationContext(_context(principal, tenant))
        return await self.invoker.invoke(name, arguments, context=context)


def _rig(config: _Config, *, scopes: _Scopes | None = None) -> _Rig:
    service = _DataService(config.policy(corpus=_SOURCE_ACL, scopes=scopes))

    async def retrieval_service() -> _DataService:
        return service

    owner = SimpleNamespace(_retrieval_service=retrieval_service)
    runtime = SimpleNamespace(
        retrieval=RetrievalFacade(owner), knowledge=KnowledgeFacade(owner), graph=GraphFacade(owner)
    )
    tools = build_reader_tool_catalog(runtime, KnowledgeReferenceStore())  # type: ignore[arg-type]
    return _Rig(ToolInvoker(tools), service)


# tool -> the arguments that make it reach its facade, and the service method it ends up calling
_TOOL_CALLS: dict[str, dict[str, object]] = {
    "vector_search": {"tenant_id": "acme", "query": "release"},
    "fetch_evidence": {"tenant_id": "acme", "items": [{"chunk_id": "chunk-1"}]},
    "get_document_context": {"tenant_id": "acme", "document_id": "document-1"},
    "list_sources": {"tenant_id": "acme"},
    "describe_graph": {},
    "graph_triplet_search": {"tenant_id": "acme", "predicate": "links_to"},
    "graph_subgraph_search": {"tenant_id": "acme", "start_node": "a"},
    "graph_path_search": {"tenant_id": "acme", "start_node": "a", "end_node": "b"},
    "resolve_graph_nodes": {
        "tenant_id": "acme",
        "selector": {"kind": "exact_title", "value": "Release runbook"},
    },
    "list_documents": {"tenant_id": "acme"},
    "find_entities": {"tenant_id": "acme", "query": "release"},
}
_DATA_METHOD = {
    "vector_search": "retrieve",
    "fetch_evidence": "read_evidence",
    "get_document_context": "get_document_context",
    "list_sources": "list_readable_sources",
    "graph_triplet_search": "search_graph_triplets",
    "graph_subgraph_search": "search_graph_subgraph",
    "graph_path_search": "search_graph_paths",
    "resolve_graph_nodes": "resolve_graph_nodes",
    "list_documents": "list_documents",
    "find_entities": "find_entities",
}
# tool -> the key of its success payload that carries what the stub returned
_DATA_KEY = {
    "vector_search": "results",
    "fetch_evidence": "items",
    "get_document_context": "chunks",
    "list_sources": "sources",
    "graph_triplet_search": "triplets",
    "graph_subgraph_search": "nodes",
    "graph_path_search": "paths",
    "resolve_graph_nodes": "candidates",
    "list_documents": "documents",
    "find_entities": "matches",
}
_STATIC_TOOL = "describe_graph"


def test_the_call_table_covers_every_reader_tool(config: _Config) -> None:
    """A new reader tool fails here until it is called, refused and served below."""

    catalog = {tool.spec.name for tool in _rig(config).invoker.tools}

    assert len(catalog) == 11
    assert set(_TOOL_CALLS) == catalog
    assert set(_DATA_METHOD) | {_STATIC_TOOL} == catalog
    assert set(_DATA_KEY) == set(_DATA_METHOD)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(_DATA_METHOD))
async def test_the_stub_serves_a_listed_principal_through_every_tool(
    config: _Config, name: str
) -> None:
    """The control: the same rig, with a listed principal, reaches the data and returns it.

    Without this, "the stub was never called" below could be a stub that returns nothing, or an
    argument the tool rejects before it gets near a facade.
    """

    config.write([_ALICE])
    rig = _rig(config)

    result = await rig.call(name, "alice-sub")

    assert result["ok"] is True
    assert result[_DATA_KEY[name]], result
    assert rig.service.reached == [_DATA_METHOD[name]]
    (held,) = rig.service.holds
    assert isinstance(held, AccessContext)
    assert held.entitled_source_scopes == frozenset(
        {"scope-conf", "scope-jira", "scope-wiki-a", "scope-wiki-b"}
    )
    assert held.corpus_mode == "source_acl"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("principal", "tenant"),
    [("mallory-sub", "acme"), ("alice-sub", "globex")],
    ids=["unlisted-principal", "unlisted-tenant"],
)
@pytest.mark.parametrize("name", sorted(_DATA_METHOD))
async def test_every_reader_tool_refuses_an_unlisted_principal(
    config: _Config, name: str, principal: str, tenant: str
) -> None:
    """With the file on, a caller it does not list reaches no data through any tool.

    Each data tool reaches its facade, whose hold refuses with the forbidden error; the stub's
    data methods are never called. The tools catch that error, so a refused caller is told the
    read "failed" and gets none of the response keys, not the 403 the doors return.
    """

    config.write([_ALICE])
    rig = _rig(config)

    try:
        result = await rig.call(name, principal, tenant)
    except HarborAuthError as refusal:
        assert refusal.forbidden is True
    else:
        assert result["ok"] is False
        assert set(result) == {"ok", "error"}
        assert principal not in str(result)

    (decision,) = rig.service.holds
    assert isinstance(decision, HarborAuthError)
    assert decision.forbidden is True
    assert str(decision) == REFUSAL
    assert rig.service.reached == []


@pytest.mark.asyncio
async def test_describe_graph_reads_no_tenant_data_so_no_facade_holds_it(
    config: _Config,
) -> None:
    """The one reader tool with no facade: its payload is the static graph contract.

    It takes no tenant and reads nothing a tenant owns, so at the tool layer there is nothing to
    refuse and it succeeds for an unlisted caller. The MCP door is what gates it, by requiring
    the principal to be listed in some tenant (``test_access_file_gate.py``).
    """

    config.write([_ALICE])
    rig = _rig(config)

    result = await rig.call(_STATIC_TOOL, "mallory-sub")

    assert result["ok"] is True
    assert set(result["versions"]) == {"structural", "semantic", "ontology"}  # type: ignore[call-overload]
    assert rig.service.holds == []
    assert rig.service.reached == []
