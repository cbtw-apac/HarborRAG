"""External link-target stubs: marked on write, pruned only when nothing references them."""

from __future__ import annotations

import pytest

from harborrag_adapters.repositories.graph.falkordb import knowledge_admin, knowledge_writes
from harborrag_adapters.repositories.graph.falkordb.knowledge_mapping import KnowledgeGraphMapper
from harborrag_adapters.repositories.graph.falkordb.knowledge_support import access_predicate
from harborrag_adapters.repositories.graph.falkordb.mapping import FalkorDBMapper
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


@pytest.mark.asyncio
async def test_pruning_pages_through_a_backlog_in_bounded_batches() -> None:
    client = FakeFalkorDBClient()
    client.read_results = [
        FakeQueryResult([HeaderItem("node_key")], [["stub-1"], ["stub-2"]]),
        FakeQueryResult([HeaderItem("node_key")], [["stub-3"]]),
    ]

    pruned = await knowledge_admin.prune_external_stubs(client, context=CONTEXT, batch_size=2)

    assert pruned == 3
    # A short page ends the pass: no third read.
    assert len(client.read_calls) == 2
    for statement, parameters in client.read_calls:
        assert "LIMIT $batch_size" in statement
        assert parameters["batch_size"] == 2
    assert [parameters["node_keys"] for _, parameters in client.write_calls] == [
        ["stub-1", "stub-2"],
        ["stub-3"],
    ]


@pytest.mark.asyncio
async def test_a_non_positive_prune_batch_is_rejected() -> None:
    with pytest.raises(ValueError):
        await knowledge_admin.prune_external_stubs(
            FakeFalkorDBClient(), context=CONTEXT, batch_size=0
        )


def test_the_graph_acl_checks_a_stub_against_the_tenant_not_its_last_writer_scope() -> None:
    # The stub key is scope-free, so the source_scope_id it carries is whichever
    # declarer was projected last; checking it flipped visibility with ingestion order.
    predicate = " ".join(access_predicate("node").split())

    assert "node.external = true AND $tenant_visible" in predicate
    assert (
        "coalesce(node.external, false) = false "
        "AND node.source_scope_id IN $authorized_source_scope_ids"
    ) in predicate


@pytest.mark.asyncio
async def test_removing_a_scope_leaves_shared_stubs_for_pruning() -> None:
    client = FakeFalkorDBClient()

    await repository(client).delete_source_scope("scope-a", context=CONTEXT)

    relations, nodes = (statement for statement, _ in client.write_calls)
    # The scope's own links go; the stub other scopes may still link to does not.
    assert "MATCH ()-[relation]->()" in relations
    assert "DETACH DELETE node" in nodes
    assert "coalesce(node.external, false) = false" in nodes


def test_a_stub_read_back_from_the_graph_maps_to_a_node_record() -> None:
    # The top-level ``external`` flag and ``stub_touched_at`` clock are write-side
    # properties; left in, the strict record rejected every read that reached a stub.
    row = FalkorDBMapper.encode_properties(
        knowledge_writes._node_row(_stub(), tenant_id="tenant-1")
    )
    row["stub_touched_at"] = 1_760_000_000_000

    node = KnowledgeGraphMapper.node(row)

    assert node.node_key == _stub().node_key
    assert node.logical_id == "RHR-2687"
