"""Census the eval corpus offline, shaped exactly like graph_health.py's Cypher returns.

Reproduces what FalkorDB holds once every corpus batch has been written: nodes merge by
node_key and relations by relation_id, the last batch's properties winning (`SET node =
row` / `SET relation = row` in the adapter's knowledge_writes.py) except that placeholder
nodes only ever fill gaps (`ON CREATE SET`) and never overwrite an existing node. Batches
are merged in the same order the live seeders write them. That equivalence is what lets a
committed offline baseline gate a change without a running graph.

Verified against a live seeded graph: every report key matches exactly except ``top_hubs``
(see its tie-break note below), identity lists included.
"""

from __future__ import annotations

from collections import Counter

from harborrag_core.ingestion import GraphEdgeRecord, GraphNodeRecord, KnowledgeNodeKind

from ..corpus import TENANT_ID, EvalCorpus
from .metrics import compute_report, connected_component_sizes


def _merge(corpus: EvalCorpus) -> tuple[dict[str, GraphNodeRecord], dict[str, GraphEdgeRecord]]:
    nodes: dict[str, GraphNodeRecord] = {}
    relations: dict[str, GraphEdgeRecord] = {}
    for batch in corpus.batches.values():
        for node in batch.nodes:
            # Placeholders share the real node's key and must never downgrade it —
            # mirrors upsert_nodes, where placeholder rows are ON CREATE SET only.
            existing = nodes.get(node.node_key)
            if node.attributes.get("placeholder") is True and existing is not None:
                continue
            # Two *concrete* records for one key make this merge order-dependent, so the
            # committed baseline would turn on fixture filenames rather than on projection
            # behaviour. The DataSource node is the known exception: every fixture
            # directory shares one source_scope_id, so all five connectors fuse onto one
            # node whose connector_type is whichever batch merged last. Splitting the
            # fixtures by scope is the real fix and moves every source-entity node key.
            if (
                existing is not None
                and existing != node
                and not existing.attributes.get("placeholder")
                and node.node_kind is not KnowledgeNodeKind.DATA_SOURCE
            ):
                raise AssertionError(f"order-dependent merge for node_key {node.node_key}")
            nodes[node.node_key] = node
        relations.update({relation.relation_id: relation for relation in batch.relations})
    # An edge whose endpoints are not both present is never written live: upsert_relations
    # MATCHes both ends and silently writes nothing. Drop it here for the same reason,
    # rather than raising on a shape the live graph tolerates.
    attached = {
        relation_id: relation
        for relation_id, relation in relations.items()
        if relation.source_node_key in nodes and relation.target_node_key in nodes
    }
    return nodes, attached


def corpus_health_entry(corpus: EvalCorpus) -> dict[str, object]:
    """One `graph_health.py --identities` report entry, computed without a graph."""

    nodes, relations = _merge(corpus)
    kinds = {node_key: node.node_kind.value for node_key, node in nodes.items()}
    edges = [(r.source_node_key, r.target_node_key) for r in relations.values()]

    node_counts = Counter((node.node_kind.value, node.entity_type.value) for node in nodes.values())
    relation_counts = Counter(
        (kinds[r.source_node_key], r.relation_type.value, kinds[r.target_node_key])
        for r in relations.values()
    )
    # `NOT (node)--()` is undirected, so an endpoint on either side of any edge counts.
    connected = {node_key for edge in edges for node_key in edge}
    orphan_counts = Counter(kinds[node_key] for node_key in nodes if node_key not in connected)
    semantic = Counter(
        (r.relation_type.value, r.source_node_key, r.target_node_key) for r in relations.values()
    )
    degrees = Counter(node_key for edge in edges for node_key in edge)

    report = compute_report(
        tenant_id=TENANT_ID,
        node_census=[
            {"kind": kind, "entity_type": entity_type, "item_count": count}
            for (kind, entity_type), count in sorted(node_counts.items())
        ],
        relation_census=[
            {
                "source_kind": source_kind,
                "relation_type": relation_type,
                "target_kind": target_kind,
                "item_count": count,
            }
            for (source_kind, relation_type, target_kind), count in sorted(relation_counts.items())
        ],
        orphan_census=[
            {"kind": kind, "item_count": count} for kind, count in sorted(orphan_counts.items())
        ],
        placeholder_count=sum(
            1
            for node in nodes.values()
            if node.node_kind is KnowledgeNodeKind.SOURCE_ENTITY
            and node.attributes.get("placeholder") is True
        ),
        duplicate_semantic_count=sum(1 for count in semantic.values() if count > 1),
        # Ties broken by node_key: the live ORDER BY degree DESC leaves equal degrees in
        # whatever order FalkorDB returns them, which a committed baseline cannot carry.
        # This is the one key that does not reproduce live, and not only in ordering --
        # ten nodes currently tie at degree 5 and LIMIT 10 admits two of them, so live and
        # offline disagree on *which* two. The degree sequence itself is identical.
        top_hubs=[
            {
                "node_key": node_key,
                "kind": kinds[node_key],
                "title": nodes[node_key].title,
                "degree": degree,
            }
            for node_key, degree in sorted(degrees.items(), key=lambda item: (-item[1], item[0]))[
                :10
            ]
        ],
        component_sizes=connected_component_sizes(nodes, edges),
    )
    entry = report.as_dict()
    entry["node_keys"] = sorted(nodes)
    entry["relation_ids"] = sorted(relations)
    return entry
