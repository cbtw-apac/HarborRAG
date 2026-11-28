"""Node resolution shows tenant-owned nodes to a reader who can read the tenant."""

from __future__ import annotations

from typing import Any

import pytest

from harborrag_core.contracts.reader import GraphNodeResolveRequest
from harborrag_core.ingestion import (
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeNodeKind,
)
from harborrag_core.retrieval import (
    GraphNodeResolutionQuery,
    GraphNodeResolutionResult,
    GraphNodeSelectorKind,
)
from harborrag_core.security import AccessContext
from harborrag_engine.retrieval.service.reader_resources import ReaderResources
from harborrag_engine.retrieval.service.readers import ReaderRetrieval

pytestmark = pytest.mark.unit

TENANT = "tenant-1"


class _Topology:
    """A reader granted ``sources``, none of whose resources are in the batch."""

    def __init__(self, sources: tuple[str, ...]) -> None:
        self.sources = sources

    async def allowed_document_ids(self, tenant_id: str, *, access: Any, limit: int = 0) -> tuple:
        del tenant_id, access, limit
        return ()

    async def allowed_source_scope_ids(
        self, tenant_id: str, *, access: Any, limit: int = 0
    ) -> tuple[str, ...]:
        del tenant_id, access, limit
        return self.sources

    async def authorized_document_ids(
        self, tenant_id: str, document_ids: tuple[str, ...], *, access: Any
    ) -> set[str]:
        del tenant_id, document_ids, access
        return set()

    async def authorized_source_scope_ids(
        self, tenant_id: str, source_scope_ids: tuple[str, ...], *, access: Any
    ) -> set[str]:
        del tenant_id, access
        return set(source_scope_ids) & set(self.sources)


class _Graph:
    def __init__(self, candidates: tuple[GraphNodeRecord, ...]) -> None:
        self.candidates = candidates

    async def resolve_nodes(
        self, query: GraphNodeResolutionQuery, *, context: Any
    ) -> GraphNodeResolutionResult:
        del query, context
        return GraphNodeResolutionResult(candidates=self.candidates, truncated=False)


def _tenant_node(key: str) -> GraphNodeRecord:
    return GraphNodeRecord(
        node_key=key,
        node_kind=KnowledgeNodeKind.TENANT,
        entity_type=GraphEntityType.TENANT,
        logical_id=key,
        ownership_scope=GraphOwnershipScope.TENANT,
        owner_id=TENANT,
        title="Demo tenant",
    )


def _reader(graph: _Graph, *, sources: tuple[str, ...]) -> ReaderRetrieval:
    topology = _Topology(sources)
    return ReaderRetrieval(
        ReaderResources(
            vectors=None,  # type: ignore[arg-type]
            validator=None,  # type: ignore[arg-type]
            permissions=None,  # type: ignore[arg-type]
            topology=topology,  # type: ignore[arg-type]
            snapshots=None,
            chunks=None,  # type: ignore[arg-type]
            sources=None,
            graph=graph,  # type: ignore[arg-type]
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sources", "expected"),
    [(("source-1",), ["tenant-node"]), ((), [])],
    ids=["tenant-reader", "no-grants"],
)
async def test_resolve_graph_nodes_of_only_tenant_nodes_follows_tenant_readability(
    sources: tuple[str, ...], expected: list[str]
) -> None:
    graph = _Graph((_tenant_node("tenant-node"),))

    response = await _reader(graph, sources=sources).resolve_graph_nodes(
        GraphNodeResolveRequest(
            AccessContext(tenant_id=TENANT, principal_id="reader-1"),
            GraphNodeResolutionQuery(
                selector_kind=GraphNodeSelectorKind.EXACT_TITLE, value="Demo tenant"
            ),
        )
    )

    assert [node.node_key for node in response.candidates] == expected
