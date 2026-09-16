"""title_key is derived from title in exactly one place, so the two cannot drift."""

from __future__ import annotations

import pytest

from harborrag_adapters.repositories.graph.falkordb import (
    FalkorDBGraphConfig,
    FalkorKnowledgeGraphRepository,
)
from harborrag_adapters.repositories.graph.falkordb.knowledge_writes import _node_row
from harborrag_core.ingestion import (
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeNodeKind,
)
from harborrag_core.schemas.storage import StorageOperationContext

from .fakes import FakeFalkorDBClient

pytestmark = pytest.mark.unit

CONTEXT = StorageOperationContext.system("tenant-a")


def _node(*, placeholder: bool = False) -> GraphNodeRecord:
    return GraphNodeRecord(
        node_key="node-1",
        node_kind=KnowledgeNodeKind.SOURCE_ENTITY,
        entity_type=GraphEntityType.CONFLUENCE_PAGE,
        logical_id="91980256",
        ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
        owner_id="tenant-a",
        source_scope_id="scope-a",
        title="Handover Document",
        attributes={"placeholder": True} if placeholder else {},
    )


def test_the_written_row_carries_no_precomputed_title_key() -> None:
    # The store derives it from the final title; a second Python computation is dead for
    # ordinary nodes and silently authoritative for placeholders -- two sources, one truth.
    assert "title_key" not in _node_row(_node(), tenant_id="tenant-a")


@pytest.mark.asyncio
async def test_both_write_paths_derive_title_key_from_the_stored_title() -> None:
    client = FakeFalkorDBClient()
    repository = FalkorKnowledgeGraphRepository(
        FalkorDBGraphConfig(),
        client=client,  # type: ignore[arg-type]
    )

    await repository.upsert_nodes([_node(), _node(placeholder=True)], context=CONTEXT)

    statements = [statement for statement, _ in client.write_calls if "MERGE" in statement]
    assert len(statements) == 2
    for statement in statements:
        assert "node.title_key = toLower(node.title)" in statement
