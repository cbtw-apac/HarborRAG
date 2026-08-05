from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from harborrag_adapters.repositories.graph.falkordb.mapping import FalkorDBMapper
from harborrag_core.ingestion import (
    GraphEdgeRecord,
    GraphNodeRecord,
    GraphProjectionVerification,
    KnowledgeGraphTraversal,
)
from harborrag_core.retrieval import GraphPath, GraphTriplet


class KnowledgeGraphMapper:
    """Map FalkorDB graph values without retaining provider-internal IDs."""

    @classmethod
    def node(cls, raw: Any) -> GraphNodeRecord:
        return GraphNodeRecord.model_validate(cls.properties(raw))

    @classmethod
    def relation(cls, raw: Any) -> GraphEdgeRecord:
        return GraphEdgeRecord.model_validate(cls.properties(raw))

    @classmethod
    def triplet(cls, row: Mapping[str, Any]) -> GraphTriplet:
        return GraphTriplet(
            subject=cls.node(row["subject"]),
            predicate=cls.relation(row["predicate"]),
            object=cls.node(row["object"]),
        )

    @classmethod
    def path(cls, row: Mapping[str, Any]) -> GraphPath:
        return GraphPath(
            nodes=tuple(cls.node(raw) for raw in row["path_nodes"]),
            relations=tuple(cls.relation(raw) for raw in row["path_relations"]),
        )

    @staticmethod
    def properties(raw: Any) -> dict[str, Any]:
        values = dict(getattr(raw, "properties", raw))
        return {
            key: FalkorDBMapper.decode_property(value)
            for key, value in values.items()
            if key != "tenant_id"
        }

    @staticmethod
    def decoded_list(value: Any) -> tuple[Any, ...]:
        decoded = FalkorDBMapper.decode_property(value)
        if isinstance(decoded, (list, tuple)):
            return tuple(decoded)
        return ()


def build_graph_verification(
    *,
    node_keys: tuple[str, ...],
    relation_ids: tuple[str, ...],
    node_rows: Sequence[Mapping[str, Any]],
    relation_rows: Sequence[Mapping[str, Any]],
) -> GraphProjectionVerification:
    """Compare a staged graph read-back with its deterministic manifest."""
    node_occurrences = {str(row["node_key"]): int(row["occurrences"]) for row in node_rows}
    relation_occurrences = {
        str(row["relation_id"]): int(row["occurrences"]) for row in relation_rows
    }
    expected_nodes = frozenset(node_keys)
    missing_nodes = tuple(sorted(expected_nodes - node_occurrences.keys()))
    missing_relations = tuple(sorted(frozenset(relation_ids) - relation_occurrences.keys()))
    dangling_relations = tuple(
        sorted(
            str(row["relation_id"])
            for row in relation_rows
            if row.get("source_node_key") not in expected_nodes
            or row.get("target_node_key") not in expected_nodes
        )
    )
    duplicate_nodes = tuple(sorted(key for key, count in node_occurrences.items() if count != 1))
    duplicate_relations = tuple(
        sorted(key for key, count in relation_occurrences.items() if count != 1)
    )
    actual_nodes = sum(node_occurrences.values())
    actual_relations = sum(relation_occurrences.values())
    valid = not any(
        (
            missing_nodes,
            missing_relations,
            dangling_relations,
            duplicate_nodes,
            duplicate_relations,
        )
    ) and (actual_nodes, actual_relations) == (len(node_keys), len(relation_ids))
    return GraphProjectionVerification(
        valid=valid,
        expected_node_count=len(node_keys),
        actual_node_count=actual_nodes,
        expected_relation_count=len(relation_ids),
        actual_relation_count=actual_relations,
        missing_node_keys=missing_nodes,
        missing_relation_ids=missing_relations,
        dangling_relation_ids=dangling_relations,
        duplicate_node_keys=duplicate_nodes,
        duplicate_relation_ids=duplicate_relations,
    )


def build_knowledge_traversal(
    rows: Sequence[Mapping[str, Any]],
    *,
    max_nodes: int,
    path_limit: int,
) -> KnowledgeGraphTraversal:
    """Deduplicate bounded path rows into canonical graph records."""
    nodes: dict[str, GraphNodeRecord] = {}
    relations: dict[str, GraphEdgeRecord] = {}
    truncated = len(rows) > path_limit
    for row in rows[:path_limit]:
        for raw_node in row["path_nodes"]:
            node = KnowledgeGraphMapper.node(raw_node)
            nodes[node.node_key] = node
            if len(nodes) >= max_nodes:
                truncated = True
                break
        for raw_relation in row["path_relations"]:
            relation = KnowledgeGraphMapper.relation(raw_relation)
            relations[relation.relation_id] = relation
        if truncated:
            break
    valid_relations = tuple(
        relation
        for relation in relations.values()
        if relation.source_node_key in nodes and relation.target_node_key in nodes
    )
    return KnowledgeGraphTraversal(
        nodes=tuple(nodes.values()),
        relations=valid_relations,
        truncated=truncated,
    )
