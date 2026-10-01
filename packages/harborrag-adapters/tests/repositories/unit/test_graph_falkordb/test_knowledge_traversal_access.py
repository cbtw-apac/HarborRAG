"""Bounded traversal applies the same graph ACL as the other knowledge reads."""

from __future__ import annotations

import pytest

from harborrag_adapters.repositories.graph.falkordb.knowledge_support import access_predicate
from harborrag_core.retrieval import GraphAccessScope
from harborrag_core.schemas.storage import StorageOperationContext

from .fakes import FakeFalkorDBClient, FakeQueryResult, HeaderItem
from .test_knowledge import repository


@pytest.mark.asyncio
async def test_traverse_applies_the_access_scope_to_start_nodes_and_relations() -> None:
    client = FakeFalkorDBClient()
    client.read_results = [
        FakeQueryResult([HeaderItem("path_nodes"), HeaderItem("path_relations")], [])
    ]

    await repository(client).traverse(
        "chunk-1",
        max_depth=2,
        max_nodes=10,
        direction="both",
        access_scope=GraphAccessScope(document_ids=("d1",)),
        context=StorageOperationContext.system("tenant-1"),
    )

    [(statement, parameters)] = client.read_calls
    assert access_predicate("start") in statement
    assert access_predicate("node") in statement
    assert access_predicate("relation") in statement
    assert statement.index("$authorized_document_ids") < statement.index("LIMIT $path_limit")
    assert parameters["authorized_document_ids"] == ["d1"]
    assert parameters["authorized_source_scope_ids"] == []
    assert parameters["access_unrestricted"] is False
    assert parameters["tenant_visible"] is True
    # The existing tenant and schema-version predicates are kept.
    assert "start.tenant_id = $tenant_id" in statement
    assert "node.graph_schema_version = $graph_schema_version" in statement
    assert "relation.tenant_id = $tenant_id" in statement
