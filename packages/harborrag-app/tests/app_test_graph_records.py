"""Canonical graph node/relation payloads shared by the app test doubles."""

from __future__ import annotations

from harborrag_runtime.sdk import RetrievalLane


def graph_records() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    """Return one document-version node, one section, and their relation."""

    subject = {
        "node_key": "document:1",
        "node_kind": "DocumentVersion",
        "entity_type": "document_version",
        "logical_id": "document:1",
        "graph_schema_version": "2.0",
        "ownership_scope": "DOCUMENT_VERSION",
        "owner_id": "tenant-1",
        "document_id": "document:1",
        "document_version_id": "document-version:1",
        "source_scope_id": "scope:1",
        "title": "Architecture",
    }
    object_node = {
        "node_key": "section:1",
        "node_kind": "Structure",
        "entity_type": "section",
        "logical_id": "section:1",
        "graph_schema_version": "2.0",
        "ownership_scope": "DOCUMENT_VERSION",
        "owner_id": "tenant-1",
        "document_id": "document:1",
        "document_version_id": "document-version:1",
        "source_scope_id": "scope:1",
        "title": "Publication",
    }
    relation = {
        "relation_id": "relation:1",
        "relation_type": "contains",
        "source_node_key": "document:1",
        "target_node_key": "section:1",
        "graph_schema_version": "2.0",
        "ownership_scope": "DOCUMENT_VERSION",
        "owner_id": "tenant-1",
        "source_scope_id": "scope:1",
        "document_id": "document:1",
        "document_version_id": "document-version:1",
        "source_relation_version": "1",
        "source_explicit": True,
    }
    return subject, object_node, relation


def graph_diagnostics(*, accepted: int) -> dict[str, object]:
    """Return the diagnostics envelope a successful graph query reports."""

    return {
        "candidate_count": accepted,
        "accepted_count": accepted,
        "stale_count": 0,
        "unpublished_count": 0,
        "projection_truncated": False,
    }


def graph_payload(operation: str) -> dict[str, object]:
    """Return the data envelope each graph operation reports on success."""

    subject, object_node, relation = graph_records()
    if operation == "triplets":
        return {
            "triplets": [{"subject": subject, "predicate": relation, "object": object_node}],
            "diagnostics": graph_diagnostics(accepted=1),
        }
    if operation == "paths":
        return {
            "paths": [{"nodes": [subject, object_node], "relations": [relation]}],
            "diagnostics": graph_diagnostics(accepted=1),
        }
    return {
        "nodes": [subject, object_node],
        "relations": [relation],
        "diagnostics": graph_diagnostics(accepted=2),
    }


def retrieval_payload(
    *,
    lane: RetrievalLane,
    top_k: int,
    include_content: bool,
    include_metadata: bool,
    score_threshold: float,
) -> dict[str, object]:
    """Return the single-hit retrieval envelope the app service double reports."""

    result: dict[str, object] = {
        "rank": 1,
        "id": "chunk-1",
        "score": 0.9,
        "source": "hybrid",
    }
    if include_content:
        result["content"] = "retrieved text"
    if include_metadata:
        result["metadata"] = {"category": "architecture"}
    return {
        "request_id": "mock-retrieval",
        "lane": lane.value,
        "results": ([result][:top_k] if score_threshold <= 0.9 else []),
        "diagnostics": {
            "vector_hits": 1,
            "graph_nodes": 2,
            "graph_edges": 1,
            "graph_hits": 1,
            "graph_truncated": False,
            "duration_ms": 1.0,
        },
    }


def projection_inventory_payload(tenant: str) -> dict[str, object]:
    """Return the projection inventory a tenant reports before deletion."""

    return {
        "tenant": tenant,
        "vector_collections": [
            {
                "logical_name": "evidence",
                "physical_name": f"{tenant}_evidence",
                "exists": True,
            },
        ],
        "graph_name": "harborrag",
        "graph_nodes": 12,
        "graph_relations": 8,
    }


__all__ = [
    "graph_diagnostics",
    "graph_payload",
    "graph_records",
    "projection_inventory_payload",
    "retrieval_payload",
]
