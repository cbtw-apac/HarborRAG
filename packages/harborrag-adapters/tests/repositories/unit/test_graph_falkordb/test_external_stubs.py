"""External link-target stubs: marked on write, pruned only when nothing references them."""

from __future__ import annotations

import pytest

from harborrag_adapters.repositories.graph.falkordb import knowledge_writes
from harborrag_core.ingestion import (
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeNodeKind,
)
from harborrag_core.storage import StorageOperationContext

from .fakes import FakeFalkorDBClient, FakeQueryResult, HeaderItem
from .test_knowledge import repository

CONTEXT = StorageOperationContext.system("tenant-1")


def _stub() -> GraphNodeRecord:
    return GraphNodeRecord(
        node_key="graph-v2-external-source-entity:stub",
        node_kind=KnowledgeNodeKind.SOURCE_ENTITY,
        entity_type=GraphEntityType.JIRA_ISSUE,
        logical_id="RHR-2687",
        ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
        owner_id="tenant-1",
        source_scope_id="scope-a",
        title="RHR-2687",
        attributes={"placeholder": True, "external": True},
    )


@pytest.mark.asyncio
async def test_a_stub_is_written_as_a_refreshed_external_placeholder() -> None:
    client = FakeFalkorDBClient()

    await knowledge_writes.upsert_nodes(client, (_stub(),), context=CONTEXT)

    statement, parameters = client.write_calls[-1]
    row = parameters["rows"][0]
    # Through the placeholder path, so it can never overwrite a concrete node...
    assert "ON CREATE SET node = row" in statement
    # ...flagged at top level, because node attributes are stored as one JSON string...
    assert row["external"] is True and row["placeholder"] is True
    # ...and touched on every write, which is what the pruning grace period reads.
    assert "node.stub_touched_at" in statement and "timestamp()" in statement


@pytest.mark.asyncio
async def test_pruning_matches_only_edgeless_external_stubs_past_the_grace_period() -> None:
    client = FakeFalkorDBClient()
    client.read_results = [
        FakeQueryResult([HeaderItem("node_key")], [["graph-v2-external-source-entity:stub"]])
    ]

    pruned = await repository(client).prune_external_stubs(context=CONTEXT, grace_seconds=60)

    assert pruned == 1
    read, read_parameters = client.read_calls[0]
    assert read_parameters["tenant_id"] == "tenant-1"
    assert read_parameters["grace_ms"] == 60_000
    write, write_parameters = client.write_calls[0]
    for statement in (read, write):
        assert "node.external = true" in statement
        assert "node.placeholder = true" in statement
        assert "NOT (node)--()" in statement
        # A plain DELETE: it cannot remove a node that has gained a relationship.
        assert "DETACH" not in statement
    assert write_parameters["node_keys"] == ["graph-v2-external-source-entity:stub"]


@pytest.mark.asyncio
async def test_pruning_writes_nothing_when_no_stub_is_orphaned() -> None:
    client = FakeFalkorDBClient()
    client.read_results = [FakeQueryResult([HeaderItem("node_key")], [])]

    assert await repository(client).prune_external_stubs(context=CONTEXT) == 0
    assert client.write_calls == []


@pytest.mark.asyncio
async def test_a_negative_grace_period_is_rejected() -> None:
    with pytest.raises(ValueError):
        await repository(FakeFalkorDBClient()).prune_external_stubs(
            context=CONTEXT, grace_seconds=-1
        )
