"""Contract tests for FakeKnowledgeGraphRepository, the core-owned graph port fake.

The port is the contract, so the first exercise of a change to it belongs here rather
than in an adapter suite. `delete_relations` gets the most attention: what it must *not*
do is the load-bearing half.
"""

from __future__ import annotations

import pytest

from harborrag_core.chunking import RelationType
from harborrag_core.ingestion import (
    GraphEdgeRecord,
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeNodeKind,
)
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_core.testing.graph_fakes import FakeKnowledgeGraphRepository

pytestmark = pytest.mark.whitebox

_CONTEXT = StorageOperationContext.system("tenant-1")


def _source_entity(node_key: str, logical_id: str) -> GraphNodeRecord:
    return GraphNodeRecord(
        node_key=node_key,
        node_kind=KnowledgeNodeKind.SOURCE_ENTITY,
        entity_type=GraphEntityType.LOCAL_FILE,
        logical_id=logical_id,
        ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
        owner_id="tenant-1",
        source_scope_id="scope-1",
    )


def _link(
    relation_id: str,
    source_node_key: str,
    target_node_key: str,
    *,
    relation_type: RelationType = RelationType.LINKS_TO,
) -> GraphEdgeRecord:
    return GraphEdgeRecord(
        relation_id=relation_id,
        relation_type=relation_type,
        source_node_key=source_node_key,
        target_node_key=target_node_key,
        ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
        owner_id="tenant-1",
        source_scope_id="scope-1",
        source_relation_version="2.0",
        source_explicit=True,
    )


async def _seeded() -> FakeKnowledgeGraphRepository:
    """One linker with two links -- one to a real item, one to a placeholder stub.

    The linker is also a member of its container, so it stays attached when its links go;
    otherwise an orphan sweep takes the whole fixture and shows nothing.
    """

    graph = FakeKnowledgeGraphRepository()
    await graph.write_projection(
        (
            _source_entity("container", "docs"),
            _source_entity("linker", "docs/a.txt"),
            _source_entity("stub", "docs/b.txt"),
            _source_entity("other", "docs/c.txt"),
        ),
        (
            _link("holds", "container", "linker", relation_type=RelationType.CONTAINS),
            _link("to-stub", "linker", "stub"),
            _link("to-other", "linker", "other"),
        ),
        context=_CONTEXT,
    )
    return graph


@pytest.mark.asyncio
async def test_delete_relations_retracts_by_id_and_leaves_every_node() -> None:
    """The whole contract: relations go, nodes stay -- including the now-edgeless one.

    Deleting the far end would mean deciding it from a degree read in one call and
    deleting it in another, while a concurrent projection may have staged that same
    placeholder without its relation yet. An implementation that "tidies up" here is the
    one this test exists to reject.
    """

    graph = await _seeded()

    await graph.delete_relations((_link("to-stub", "linker", "stub"),), context=_CONTEXT)

    assert set(graph.relations) == {"holds", "to-other"}
    assert set(graph.nodes) == {"container", "linker", "stub", "other"}
    assert [relation.relation_id for relation in graph.retracted_relations] == ["to-stub"]
    # And the node it left behind reaches nothing, which is why leaving it is safe.
    traversal = await graph.traverse(
        "stub", max_depth=2, max_nodes=10, direction="both", context=_CONTEXT
    )
    assert traversal.nodes == () and traversal.relations == ()


@pytest.mark.asyncio
async def test_delete_relations_is_idempotent_and_empty_input_is_a_no_op() -> None:
    """Repair calls this on every document it repairs, most of which supersede nothing."""

    graph = await _seeded()

    await graph.delete_relations((), context=_CONTEXT)
    assert set(graph.relations) == {"holds", "to-stub", "to-other"}
    assert graph.retracted_relations == []

    retraction = (_link("to-stub", "linker", "stub"),)
    await graph.delete_relations(retraction, context=_CONTEXT)
    await graph.delete_relations(retraction, context=_CONTEXT)
    assert set(graph.relations) == {"holds", "to-other"}


@pytest.mark.asyncio
async def test_delete_relations_rejects_another_tenants_relation() -> None:
    """Mirrors the FalkorDB repository: the owner is checked, not assumed."""

    graph = await _seeded()

    with pytest.raises(ValueError, match="owner does not match"):
        await graph.delete_relations(
            (_link("to-stub", "linker", "stub"),),
            context=StorageOperationContext.system("tenant-2"),
        )

    assert set(graph.relations) == {"holds", "to-stub", "to-other"}
    assert graph.retracted_relations == []


@pytest.mark.asyncio
async def test_cleanup_is_where_the_edgeless_stub_is_reaped() -> None:
    """The other half of the deal: retraction defers, cleanup collects.

    `delete_source_item` is a cleanup path -- it runs when an item is retired, not while
    projections are being staged -- so the tenant-wide orphan sweep is safe there.
    """

    graph = await _seeded()
    await graph.delete_relations((_link("to-stub", "linker", "stub"),), context=_CONTEXT)

    await graph.delete_source_item("other", context=_CONTEXT)

    # "other" went with the item, "to-other" with it, and the stub the earlier retraction
    # left edgeless goes with the sweep. The linker survives on its container edge.
    assert set(graph.nodes) == {"container", "linker"}
    assert set(graph.relations) == {"holds"}
    assert await graph.tenant_projection_counts(context=_CONTEXT) == (2, 1)
