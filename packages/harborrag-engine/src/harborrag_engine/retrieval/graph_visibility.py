"""Permission filtering and connectivity repair for structural graph reads."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Protocol

from harborrag_core.ingestion import GraphEdgeRecord, GraphNodeRecord, GraphOwnershipScope
from harborrag_core.retrieval import GraphDirection
from harborrag_core.security import AccessContext
from harborrag_core.storage import StorageOperationContext

from .graph_metadata import selector_matches


class GraphVisibilityAuthorizer(Protocol):
    """Resolve graph ownership scopes against canonical reader permissions."""

    async def authorized_document_ids(
        self,
        tenant_id: str,
        document_ids: tuple[str, ...],
        *,
        access: AccessContext,
    ) -> set[str]: ...

    async def authorized_source_scope_ids(
        self,
        tenant_id: str,
        source_scope_ids: tuple[str, ...],
        *,
        access: AccessContext,
    ) -> set[str]: ...


async def apply_graph_permissions(
    records: Mapping[str, GraphNodeRecord | GraphEdgeRecord],
    states: dict[str, str],
    authorizer: GraphVisibilityAuthorizer | None,
    context: StorageOperationContext,
) -> dict[str, str]:
    """Mark inaccessible records without exposing denied counts to diagnostics."""

    if authorizer is None:
        return states
    document_ids = tuple(
        dict.fromkeys(
            str(record.document_id)
            for record in records.values()
            if record.document_id is not None
        )
    )
    source_ids = tuple(
        dict.fromkeys(
            record.source_scope_id
            for record in records.values()
            if record.source_scope_id is not None
        )
    )
    allowed_documents, allowed_sources = await asyncio.gather(
        authorizer.authorized_document_ids(
            str(context.tenant_id), document_ids, access=context.access
        ),
        authorizer.authorized_source_scope_ids(
            str(context.tenant_id), source_ids, access=context.access
        ),
    )
    tenant_visible = bool(allowed_documents or allowed_sources)
    for key, record in records.items():
        if not _record_authorized(
            record, allowed_documents, allowed_sources, tenant_visible
        ):
            states[key] = "denied"
    return states


def reachable_subgraph(
    nodes: Sequence[GraphNodeRecord],
    relations: Sequence[GraphEdgeRecord],
    *,
    selector: str,
    direction: GraphDirection,
    max_nodes: int,
) -> tuple[tuple[GraphNodeRecord, ...], tuple[GraphEdgeRecord, ...], int]:
    """Keep only the visible neighborhood still reachable without hidden intermediates."""

    seed = next((node for node in nodes if selector_matches(node, selector)), None)
    if seed is None:
        return (), (), 0
    node_by_key = {node.node_key: node for node in nodes}
    order = {node.node_key: index for index, node in enumerate(nodes)}
    neighbors: dict[str, set[str]] = {key: set() for key in node_by_key}
    for relation in relations:
        source = relation.source_node_key
        target = relation.target_node_key
        if source not in node_by_key or target not in node_by_key:
            continue
        if direction in {GraphDirection.OUTGOING, GraphDirection.BOTH}:
            neighbors[source].add(target)
        if direction in {GraphDirection.INCOMING, GraphDirection.BOTH}:
            neighbors[target].add(source)
    reached: list[str] = []
    queued = {seed.node_key}
    frontier = [seed.node_key]
    while frontier:
        current = frontier.pop(0)
        reached.append(current)
        for neighbor in sorted(neighbors[current], key=order.__getitem__):
            if neighbor not in queued:
                queued.add(neighbor)
                frontier.append(neighbor)
    selected_keys = set(reached[:max_nodes])
    selected_nodes = tuple(node_by_key[key] for key in reached[:max_nodes])
    selected_relations = tuple(
        relation
        for relation in relations
        if relation.source_node_key in selected_keys
        and relation.target_node_key in selected_keys
    )
    return selected_nodes, selected_relations, len(reached)


def _record_authorized(
    record: GraphNodeRecord | GraphEdgeRecord,
    document_ids: set[str],
    source_scope_ids: set[str],
    tenant_visible: bool,
) -> bool:
    if record.document_id is not None:
        return str(record.document_id) in document_ids
    if record.source_scope_id is not None:
        return record.source_scope_id in source_scope_ids
    return record.ownership_scope == GraphOwnershipScope.TENANT and tenant_visible


__all__ = ["GraphVisibilityAuthorizer", "apply_graph_permissions", "reachable_subgraph"]
