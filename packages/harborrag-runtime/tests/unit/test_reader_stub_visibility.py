"""Node resolution must not show or hide a shared external stub by ingestion order."""

from __future__ import annotations

import pytest
from test_reader_retrieval import ACCESS, Permissions, _reader

from harborrag_core.ingestion import (
    DocumentIdentityBuilder,
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
from harborrag_runtime.contracts import GraphNodeResolveRequest

_STUB_KEY = DocumentIdentityBuilder().external_source_entity_node_key(
    tenant_id="tenant-1",
    connector_type="jira",
    connection_id="jira-main",
    source_item_id="jira://EXT/EXT-1",
)


def _stub(last_writer_scope: str) -> GraphNodeRecord:
    # As read back from FalkorDB: a shared source entity comes back without attributes.
    return GraphNodeRecord(
        node_key=_STUB_KEY,
        node_kind=KnowledgeNodeKind.SOURCE_ENTITY,
        entity_type=GraphEntityType.JIRA_ISSUE,
        logical_id="EXT-1",
        ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
        owner_id="tenant-1",
        source_scope_id=last_writer_scope,
        title="EXT-1",
    )


class _StubGraph:
    """Returns the stub, as the store's ACL now does for any reader of the tenant."""

    def __init__(self, stub: GraphNodeRecord) -> None:
        self.stub = stub

    async def resolve_nodes(self, query, *, context):
        del query, context
        return GraphNodeResolutionResult(candidates=(self.stub,))


async def _resolved(permissions: Permissions, last_writer_scope: str) -> list[str]:
    response = await _reader(permissions, _StubGraph(_stub(last_writer_scope))).resolve_graph_nodes(
        GraphNodeResolveRequest(
            ACCESS,
            GraphNodeResolutionQuery(
                selector_kind=GraphNodeSelectorKind.PROVIDER_ID, value="EXT-1"
            ),
        )
    )
    return [node.node_key for node in response.candidates]


@pytest.mark.asyncio
@pytest.mark.parametrize("last_writer_scope", ["source-1", "source-2"])
async def test_a_reader_of_one_scope_resolves_the_stub_whoever_wrote_it_last(
    last_writer_scope: str,
) -> None:
    assert await _resolved(Permissions(("source-1",)), last_writer_scope) == [_STUB_KEY]


@pytest.mark.asyncio
async def test_an_ordinary_entity_of_another_scope_stays_hidden() -> None:
    ordinary = _stub("source-2").model_copy(update={"node_key": "graph-v2-source-entity:x"})
    response = await _reader(Permissions(("source-1",)), _StubGraph(ordinary)).resolve_graph_nodes(
        GraphNodeResolveRequest(
            ACCESS,
            GraphNodeResolutionQuery(
                selector_kind=GraphNodeSelectorKind.PROVIDER_ID, value="EXT-1"
            ),
        )
    )

    assert response.candidates == ()
