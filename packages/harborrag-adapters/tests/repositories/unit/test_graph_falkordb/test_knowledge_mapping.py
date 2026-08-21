from __future__ import annotations

from harborrag_adapters.repositories.graph.falkordb.knowledge_mapping import (
    KnowledgeGraphMapper,
    build_knowledge_traversal,
)
from harborrag_core.chunking import RelationType
from harborrag_core.ingestion import (
    GraphEdgeRecord,
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeNodeKind,
)


def _node(key: str) -> dict[str, object]:
    record = GraphNodeRecord(
        node_key=key,
        node_kind=KnowledgeNodeKind.SOURCE_ENTITY,
        entity_type=GraphEntityType.GENERIC_SOURCE_ITEM,
        logical_id=key,
        ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
        owner_id="tenant-1",
        source_scope_id="scope-1",
        title=key,
    )
    # Mirrors exactly what knowledge_writes._node_row stores: every node written through
    # the normal write path carries a top-level tenant_id and placeholder property that
    # is not part of the GraphNodeRecord contract.
    return {**record.model_dump(mode="json"), "tenant_id": "tenant-1", "placeholder": False}


def _relation(relation_id: str, source: str, target: str) -> dict[str, object]:
    record = GraphEdgeRecord(
        relation_id=relation_id,
        relation_type=RelationType.CONTAINS,
        source_node_key=source,
        target_node_key=target,
        ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
        owner_id="tenant-1",
        source_scope_id="scope-1",
        source_relation_version="v1",
        source_explicit=True,
    )
    return {**record.model_dump(mode="json"), "tenant_id": "tenant-1"}


def test_properties_strips_tenant_id_and_placeholder_written_by_every_node_row() -> None:
    # knowledge_writes._node_row stamps "placeholder" onto every node it writes, real or
    # not (see its comment on why). GraphNodeRecord is a StrictModel, so leaving either
    # write-only key in would make model_validate raise on every single node read back.
    node = KnowledgeGraphMapper.node(_node("n1"))

    assert node.node_key == "n1"


def test_build_knowledge_traversal_keeps_accumulating_past_the_row_limit() -> None:
    # Regression: when the store returns more path rows than path_limit (the common case
    # once the real neighborhood is large), a single "truncated" flag used both to record
    # that fact AND to end the accumulation loop caused the loop to stop after just the
    # first row - producing a near-empty subgraph even though max_nodes was nowhere close
    # to being reached.
    rows = [
        {"path_nodes": [_node("root"), _node(f"n{i}")], "path_relations": [_relation(f"r{i}", "root", f"n{i}")]}
        for i in range(5)
    ]

    traversal = build_knowledge_traversal(rows, max_nodes=10, path_limit=3)

    assert {node.node_key for node in traversal.nodes} == {"root", "n0", "n1", "n2"}
    assert len(traversal.relations) == 3
    assert traversal.truncated is True


def test_build_knowledge_traversal_stops_at_max_nodes_within_the_row_budget() -> None:
    rows = [
        {"path_nodes": [_node("root"), _node(f"n{i}")], "path_relations": [_relation(f"r{i}", "root", f"n{i}")]}
        for i in range(5)
    ]

    traversal = build_knowledge_traversal(rows, max_nodes=3, path_limit=10)

    assert len(traversal.nodes) == 3
    assert traversal.truncated is True


def test_build_knowledge_traversal_not_truncated_when_everything_fits() -> None:
    rows = [
        {"path_nodes": [_node("root"), _node("n0")], "path_relations": [_relation("r0", "root", "n0")]},
    ]

    traversal = build_knowledge_traversal(rows, max_nodes=10, path_limit=10)

    assert {node.node_key for node in traversal.nodes} == {"root", "n0"}
    assert traversal.truncated is False
