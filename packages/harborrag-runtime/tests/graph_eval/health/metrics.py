"""Pure whole-graph health computation over FalkorDB census rows."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from harborrag_core.chunking.schemas import PROJECTED_RELATION_TYPES
from harborrag_core.ingestion import KnowledgeNodeKind

_ALLOWED_RELATION_TYPES = frozenset(item.value for item in PROJECTED_RELATION_TYPES)
_ALLOWED_NODE_KINDS = frozenset(item.value for item in KnowledgeNodeKind)
# Version-owned nodes are created connected by the builder; a disconnected one is a
# projection or cleanup bug. Source-scope orphans are legitimate (placeholders, pruning
# races) and are reported, not gated.
_GATED_ORPHAN_KINDS = frozenset(
    {
        KnowledgeNodeKind.DOCUMENT_VERSION.value,
        KnowledgeNodeKind.STRUCTURE.value,
        KnowledgeNodeKind.CHUNK.value,
    }
)

# Any, not object: census values arrive from the driver as untyped scalars and are
# narrowed at use (str()/int()). Mapping[str, object] would make every narrowing an
# arg-type error under strict, and this module is outside the repo's mypy include path,
# so nothing would catch it.
Row = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class GraphHealthReport:
    tenant_id: str
    node_count: int
    relation_count: int
    nodes_by_kind: dict[str, int]
    nodes_by_entity_type: dict[str, int]
    relations_by_type: dict[str, int]
    signature_census: dict[str, int]
    orphans_by_kind: dict[str, int]
    placeholder_count: int
    duplicate_semantic_count: int
    component_sizes: tuple[int, ...]
    top_hubs: tuple[dict[str, object], ...]
    gate_failures: tuple[str, ...] = field(default=())

    @property
    def orphan_source_entity_count(self) -> int:
        return self.orphans_by_kind.get(KnowledgeNodeKind.SOURCE_ENTITY.value, 0)

    @property
    def average_degree(self) -> float:
        return (2 * self.relation_count / self.node_count) if self.node_count else 0.0

    def as_dict(self) -> dict[str, object]:
        # Copies, not references: callers extend/mutate payloads (e.g. --identities),
        # and the frozen dataclass must not change underneath them.
        return {
            "tenant_id": self.tenant_id,
            "node_count": self.node_count,
            "relation_count": self.relation_count,
            "nodes_by_kind": dict(self.nodes_by_kind),
            "nodes_by_entity_type": dict(self.nodes_by_entity_type),
            "relations_by_type": dict(self.relations_by_type),
            "signature_census": dict(self.signature_census),
            "orphans_by_kind": dict(self.orphans_by_kind),
            "placeholder_count": self.placeholder_count,
            "duplicate_semantic_count": self.duplicate_semantic_count,
            "average_degree": self.average_degree,
            "component_sizes": list(self.component_sizes),
            "top_hubs": [dict(hub) for hub in self.top_hubs],
            "gate_failures": list(self.gate_failures),
        }


def compute_report(  # noqa: PLR0913 - one keyword argument per census the Cypher returns
    *,
    tenant_id: str,
    node_census: Sequence[Row],
    relation_census: Sequence[Row],
    orphan_census: Sequence[Row],
    placeholder_count: int,
    duplicate_semantic_count: int,
    top_hubs: Sequence[Row],
    component_sizes: Sequence[int],
) -> GraphHealthReport:
    nodes_by_kind: dict[str, int] = {}
    nodes_by_entity_type: dict[str, int] = {}
    for row in node_census:
        kind = str(row["kind"])
        entity_type = str(row["entity_type"])
        count = int(row["item_count"])
        nodes_by_kind[kind] = nodes_by_kind.get(kind, 0) + count
        nodes_by_entity_type[entity_type] = nodes_by_entity_type.get(entity_type, 0) + count

    relations_by_type: dict[str, int] = {}
    signature_census: dict[str, int] = {}
    for row in relation_census:
        relation_type = str(row["relation_type"])
        count = int(row["item_count"])
        relations_by_type[relation_type] = relations_by_type.get(relation_type, 0) + count
        signature = f"{row['source_kind']} {relation_type} {row['target_kind']}"
        signature_census[signature] = signature_census.get(signature, 0) + count

    orphans_by_kind = {str(row["kind"]): int(row["item_count"]) for row in orphan_census}

    node_count = sum(nodes_by_kind.values())

    failures: list[str] = []
    # The gates below all fire on the presence of a bad row, so an empty census would
    # otherwise report a perfectly healthy graph -- and "ingestion produced nothing" is
    # exactly what a post-deploy smoke run exists to catch.
    if not node_count:
        failures.append("graph empty: no nodes for tenant")
    for relation_type in sorted(set(relations_by_type) - _ALLOWED_RELATION_TYPES):
        failures.append(f"relation type outside projected vocabulary: {relation_type}")
    for kind in sorted(set(nodes_by_kind) - _ALLOWED_NODE_KINDS):
        failures.append(f"node kind outside schema: {kind}")
    for kind in sorted(set(orphans_by_kind) & _GATED_ORPHAN_KINDS):
        failures.append(f"orphan version-owned nodes: {kind}={orphans_by_kind[kind]}")
    if duplicate_semantic_count:
        failures.append(f"duplicate semantic relations: {duplicate_semantic_count}")

    return GraphHealthReport(
        tenant_id=tenant_id,
        node_count=node_count,
        relation_count=sum(relations_by_type.values()),
        nodes_by_kind=nodes_by_kind,
        nodes_by_entity_type=nodes_by_entity_type,
        relations_by_type=relations_by_type,
        signature_census=signature_census,
        orphans_by_kind=orphans_by_kind,
        placeholder_count=placeholder_count,
        duplicate_semantic_count=duplicate_semantic_count,
        component_sizes=tuple(component_sizes),
        top_hubs=tuple(dict(row) for row in top_hubs),
        gate_failures=tuple(failures),
    )


def connected_component_sizes(
    node_keys: Iterable[str], edges: Iterable[tuple[str, str]]
) -> tuple[int, ...]:
    """Sizes of multi-node connected components, largest first.

    Client-side union-find: FalkorDB's WCC procedure cannot be tenant-scoped, and
    at smoke scale the per-tenant node/edge lists are small. Singleton components
    are the orphan census's finding, not this one's. Report-only until baselines
    justify a gate (a healthy tenant should show exactly one entry).
    """

    parent = {key: key for key in node_keys}

    def find(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for source, target in edges:
        if source in parent and target in parent:
            parent[find(source)] = find(target)
    counts: dict[str, int] = {}
    for key in parent:
        root = find(key)
        counts[root] = counts.get(root, 0) + 1
    return tuple(sorted((size for size in counts.values() if size > 1), reverse=True))
