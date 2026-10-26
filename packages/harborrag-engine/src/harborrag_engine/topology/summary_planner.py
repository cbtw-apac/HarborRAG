"""Select an acyclic aggregation forest from canonical structural manifests."""

from collections import defaultdict
from dataclasses import dataclass, field

from harborrag_core.chunking import ChunkRecord, RecordKind
from harborrag_core.ingestion import GraphEdgeRecord, GraphNodeRecord, KnowledgeNodeKind
from harborrag_core.summaries import SummaryKind

_KINDS: dict[KnowledgeNodeKind, SummaryKind] = {
    KnowledgeNodeKind.STRUCTURE: "Structure",
    KnowledgeNodeKind.DOCUMENT_VERSION: "DocumentVersion",
    KnowledgeNodeKind.SOURCE_ENTITY: "SourceEntity",
    KnowledgeNodeKind.DATA_SOURCE: "DataSource",
    KnowledgeNodeKind.TENANT: "Tenant",
}
_AGGREGATION_EDGES = {
    "HAS_DATA_SOURCE",
    "CONTAINS",
    "PARENT_OF",
    "HAS_ATTACHMENT",
    "HAS_VERSION",
    "HAS_CHUNK",
}


@dataclass(frozen=True)
class SummaryPlanNode:
    node: GraphNodeRecord
    kind: SummaryKind
    children: tuple[str, ...]
    direct_chunks: tuple[ChunkRecord, ...]
    input_chunk_ids: tuple[str, ...]
    document_ids: tuple[str, ...]


def summary_plan(
    nodes: tuple[GraphNodeRecord, ...],
    edges: tuple[GraphEdgeRecord, ...],
    chunks: tuple[ChunkRecord, ...],
    *,
    include_tenant: bool = False,
) -> tuple[SummaryPlanNode, ...]:
    """Own each chunk once; ignore navigation cross-links and reject owner cycles.

    A table/comment owns its content before a section; a nested container owns a
    contribution before the document or datasource that also exposes it.
    """
    by_key = {node.node_key: node for node in nodes}
    if len(by_key) != len(nodes):
        raise ValueError("summary plan requires unique graph node identities")
    evidence = {
        str(chunk.chunk_id): chunk for chunk in chunks if chunk.record_kind == RecordKind.EVIDENCE
    }
    if len(evidence) != sum(chunk.record_kind == RecordKind.EVIDENCE for chunk in chunks):
        raise ValueError("summary plan contains duplicate evidence identities")
    parents: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        if (
            edge.relation_type.value.upper() in _AGGREGATION_EDGES
            and edge.source_node_key in by_key
            and edge.target_node_key in by_key
            and by_key[edge.source_node_key].node_kind in _KINDS
        ):
            parents[edge.target_node_key].add(edge.source_node_key)

    def preference(key: str) -> tuple[int, int, str, str]:
        node = by_key[key]
        rank = {"Structure": 4, "DocumentVersion": 3, "SourceEntity": 2, "DataSource": 1}
        specificity = 1 if node.entity_type.value in {"table", "comment"} else 0
        return (
            -rank.get(node.node_kind.value, 0),
            -specificity - len(node.section_path),
            node.logical_id,
            key,
        )

    children: dict[str, list[str]] = defaultdict(list)
    for key, candidates in parents.items():
        children[min(candidates, key=preference)].append(key)
    owned_evidence = {key for values in children.values() for key in values if key in evidence}
    if owned_evidence != set(evidence):
        raise ValueError("canonical graph does not assign every evidence chunk an owner")
    planner = _Planner(
        by_key,
        evidence,
        children,
        {str(chunk.chunk_id): index for index, chunk in enumerate(chunks)},
        include_tenant,
    )
    for key in sorted(by_key):
        planner.visit(key)
    return tuple(planner.result)


@dataclass
class _Planner:
    by_key: dict[str, GraphNodeRecord]
    evidence: dict[str, ChunkRecord]
    children: dict[str, list[str]]
    chunk_order: dict[str, int]
    include_tenant: bool
    visiting: set[str] = field(default_factory=set)
    completed: dict[str, SummaryPlanNode] = field(default_factory=dict)
    result: list[SummaryPlanNode] = field(default_factory=list)

    def visit(self, key: str) -> None:
        if key in self.completed or self.by_key[key].node_kind not in _KINDS:
            return
        if key in self.visiting:
            raise ValueError("summary aggregation ownership contains a cycle")
        self.visiting.add(key)
        child_keys = tuple(
            sorted(
                (value for value in self.children[key] if self.by_key[value].node_kind in _KINDS),
                key=lambda value: (
                    self.by_key[value].section_path,
                    self.by_key[value].logical_id,
                    value,
                ),
            )
        )
        for child in child_keys:
            self.visit(child)
        direct = tuple(
            self.evidence[value] for value in self.children[key] if value in self.evidence
        )
        # Artifact order is the canonical content order, independent of graph edge order.
        direct_ids = {str(value.chunk_id) for value in direct}
        direct = tuple(sorted(direct, key=lambda value: self.chunk_order[str(value.chunk_id)]))
        input_ids = tuple(
            sorted(
                {
                    *direct_ids,
                    *(
                        item
                        for child in child_keys
                        for item in self.completed[child].input_chunk_ids
                    ),
                }
            )
        )
        documents = {str(value.document_id) for value in direct}
        documents.update(
            item for child in child_keys for item in self.completed[child].document_ids
        )
        node = self.by_key[key]
        if node.document_id is not None:
            documents.add(str(node.document_id))
        planned = SummaryPlanNode(
            node, _KINDS[node.node_kind], child_keys, direct, input_ids, tuple(sorted(documents))
        )
        self.completed[key] = planned
        self.visiting.remove(key)
        if self.include_tenant or node.node_kind != KnowledgeNodeKind.TENANT:
            self.result.append(planned)
