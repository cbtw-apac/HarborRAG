from __future__ import annotations

import pytest

from harborrag_core.ingestion import (
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeNodeKind,
)
from harborrag_core.schemas.storage import StorageOperationContext

from .fakes import FakeFalkorDBClient
from .test_knowledge import nodes, repository


@pytest.mark.asyncio
async def test_placeholder_nodes_only_fill_gaps_and_never_overwrite() -> None:
    # A placeholder shares the real node's key so the concrete projection can claim
    # it later. Writing it with a plain SET would downgrade an existing concrete
    # node to a stub whenever another document's batch re-projects.
    client = FakeFalkorDBClient()
    graph = repository(client)
    context = StorageOperationContext.system(tenant_id="tenant-1")
    placeholder = GraphNodeRecord(
        node_key="node-parent-page",
        node_kind=KnowledgeNodeKind.SOURCE_ENTITY,
        entity_type=GraphEntityType.CONFLUENCE_PAGE,
        logical_id="page-1",
        ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
        owner_id="tenant-1",
        source_scope_id="scope-1",
        title="page-1",
        attributes={"placeholder": True},
    )

    await graph.write_projection((*nodes(), placeholder), (), context=context)

    placeholder_calls = [
        (statement, parameters)
        for statement, parameters in client.write_calls
        if any(row["node_key"] == "node-parent-page" for row in parameters.get("rows", ()))
    ]
    assert placeholder_calls
    for statement, parameters in placeholder_calls:
        assert "ON CREATE SET node = row" in statement
        # The refresh path must stay gated on the existing node being a placeholder.
        assert "ON MATCH SET node = CASE WHEN node.placeholder = true" in statement
        assert all(
            row["placeholder"] is True
            for row in parameters["rows"]
            if row["node_key"] == "node-parent-page"
        )
    concrete = [
        (statement, parameters)
        for statement, parameters in client.write_calls
        if any(row["node_key"] == "node-document" for row in parameters.get("rows", ()))
    ]
    assert concrete
    for statement, parameters in concrete:
        assert "ON CREATE" not in statement
        assert all(row["placeholder"] is False for row in parameters["rows"])
