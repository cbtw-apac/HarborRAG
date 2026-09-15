from __future__ import annotations

import pytest

from harborrag_adapters.repositories.graph.falkordb import (
    FalkorDBGraphConfig,
    FalkorKnowledgeGraphRepository,
)
from harborrag_core.ingestion import (
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeNodeKind,
)
from harborrag_core.retrieval import (
    GraphAccessScope,
    GraphNodeResolutionQuery,
    GraphNodeSelectorKind,
)
from harborrag_core.storage import StorageOperationContext

from .fakes import FakeFalkorDBClient, FakeQueryResult, HeaderItem


@pytest.mark.asyncio
async def test_exact_node_resolution_preserves_ambiguity_and_parameterizes_scopes() -> None:
    client = FakeFalkorDBClient()
    records = tuple(
        GraphNodeRecord(
            node_key=f"node-{index}",
            node_kind=KnowledgeNodeKind.SOURCE_ENTITY,
            entity_type=GraphEntityType.GITHUB_REPOSITORY,
            logical_id=f"repo-{index}",
            ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
            owner_id="tenant-1",
            source_scope_id="source-1",
            title="Payments",
        )
        for index in (1, 2)
    )
    client.read_results = [
        FakeQueryResult(
            [HeaderItem("node")],
            [[record.model_dump(mode="json")] for record in records],
        )
    ]
    repository = FalkorKnowledgeGraphRepository(
        FalkorDBGraphConfig(),
        client=client,  # type: ignore[arg-type]
    )

    result = await repository.resolve_nodes(
        GraphNodeResolutionQuery(
            selector_kind=GraphNodeSelectorKind.EXACT_TITLE,
            value="Payments",
            source_scope_ids=("source-1",),
            access_scope=GraphAccessScope(source_scope_ids=("source-1",)),
            limit=2,
        ),
        context=StorageOperationContext.system("tenant-1"),
    )

    statement, parameters = client.read_calls[0]
    assert "node.title_key = $title_key" in statement
    assert "Payments" not in statement
    assert parameters["title_key"] == "payments"
    assert parameters["source_scope_ids"] == ["source-1"]
    assert parameters["authorized_source_scope_ids"] == ["source-1"]
    assert parameters["authorized_document_ids"] == []
    assert parameters["access_unrestricted"] is False
    assert statement.index("$authorized_source_scope_ids") < statement.index("LIMIT $limit")
    assert [item.node_key for item in result.candidates] == ["node-1", "node-2"]
