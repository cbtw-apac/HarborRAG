"""Corruption-detection paths in the staged-graph validator.

`GraphValidationService` is the gate between a staged graph write and
generation activation. The planner suites cover the agreement case; this module
corrupts a validated plan one property at a time and asserts each mismatch is
actually reported, so a silently wrong graph cannot be promoted.
"""

from __future__ import annotations

from harborrag_engine.ingestion.indexing import GraphMutationPlanner
from harborrag_engine.ingestion.indexing.graph.validation import GraphValidationService

from .test_indexing_graph import make_graph_request


def _plan_and_records():
    plan = GraphMutationPlanner().plan(make_graph_request())
    return plan, plan.nodes, plan.edges


def _errors(plan, nodes, edges) -> list[str]:
    return list(GraphValidationService().validate(plan, nodes, edges).errors)


def _chunk_node(nodes):
    return next(node for node in nodes if "Chunk" in node.labels)


def _without(nodes, target):
    return tuple(node for node in nodes if node.id != target.id)


def test_a_faithfully_persisted_graph_validates() -> None:
    plan, nodes, edges = _plan_and_records()

    result = GraphValidationService().validate(plan, nodes, edges)

    assert result.errors == ()
    assert result.valid is True


def test_duplicate_identifiers_are_reported() -> None:
    plan, nodes, edges = _plan_and_records()

    node_errors = _errors(plan, (*nodes, nodes[0]), edges)
    edge_errors = _errors(plan, nodes, (*edges, edges[0]))

    assert any("duplicate node IDs" in error for error in node_errors)
    assert any("duplicate edge IDs" in error for error in edge_errors)


def test_missing_and_unexpected_records_are_reported() -> None:
    plan, nodes, edges = _plan_and_records()
    dropped = nodes[0]
    extra = nodes[0].model_copy(update={"id": "unexpected-node"})

    missing_errors = _errors(plan, _without(nodes, dropped), edges)
    unexpected_errors = _errors(plan, (*nodes, extra), edges)

    assert any("graph nodes are missing" in error for error in missing_errors)
    assert any("unexpected graph nodes were returned" in error for error in unexpected_errors)


def test_missing_and_unexpected_edges_are_reported() -> None:
    plan, nodes, edges = _plan_and_records()
    extra = edges[0].model_copy(update={"id": "unexpected-edge"})

    missing_errors = _errors(plan, nodes, edges[1:])
    unexpected_errors = _errors(plan, nodes, (*edges, extra))

    assert any("graph edges are missing" in error for error in missing_errors)
    assert any("unexpected graph edges were returned" in error for error in unexpected_errors)


def test_altered_node_labels_and_properties_are_reported() -> None:
    plan, nodes, edges = _plan_and_records()
    target = nodes[0]
    relabelled = target.model_copy(update={"labels": {"Tampered"}})
    reproperty = target.model_copy(
        update={"properties": {**target.properties, "artifact_id": "other-artifact"}}
    )

    label_errors = _errors(plan, (relabelled, *_without(nodes, target)), edges)
    property_errors = _errors(plan, (reproperty, *_without(nodes, target)), edges)

    assert any("labels do not match" in error for error in label_errors)
    assert any("does not match" in error for error in property_errors)


def test_a_prematurely_activated_node_is_reported() -> None:
    """A staged generation must never be persisted as already active."""
    plan, nodes, edges = _plan_and_records()
    target = nodes[0]
    activated = target.model_copy(
        update={"properties": {**target.properties, "is_active": True, "index_state": "active"}}
    )

    errors = _errors(plan, (activated, *_without(nodes, target)), edges)

    assert any("property 'is_active' is invalid" in error for error in errors)
    assert any("property 'index_state' is invalid" in error for error in errors)


def test_chunk_capsule_violations_are_reported() -> None:
    plan, nodes, edges = _plan_and_records()
    chunk = _chunk_node(nodes)
    others = _without(nodes, chunk)

    no_preview = chunk.model_copy(
        update={"properties": {k: v for k, v in chunk.properties.items() if k != "preview"}}
    )
    oversized = chunk.model_copy(
        update={
            "properties": {
                **chunk.properties,
                "preview": "x" * (plan.capsule_maximum_characters + 1),
            }
        }
    )
    with_content = chunk.model_copy(
        update={"properties": {**chunk.properties, "content": "the full chunk body"}}
    )
    without_identity = chunk.model_copy(
        update={"properties": {**chunk.properties, "content_hash": ""}}
    )

    assert any(
        "has no bounded preview" in error for error in _errors(plan, (no_preview, *others), edges)
    )
    assert any(
        "preview exceeds configured bound" in error
        for error in _errors(plan, (oversized, *others), edges)
    )
    assert any(
        "contains unrestricted content" in error
        for error in _errors(plan, (with_content, *others), edges)
    )
    assert any(
        "capsule is missing content_hash" in error
        for error in _errors(plan, (without_identity, *others), edges)
    )


def test_rewired_edge_structure_is_reported() -> None:
    plan, nodes, edges = _plan_and_records()
    target = edges[0]
    rewired = target.model_copy(update={"relationship_type": "TAMPERED"})
    others = tuple(edge for edge in edges if edge.id != target.id)

    errors = _errors(plan, nodes, (rewired, *others))

    assert any("structure does not match" in error for error in errors)


def test_an_edge_to_an_unknown_node_is_reported() -> None:
    plan, nodes, edges = _plan_and_records()
    target = edges[0]
    dangling = target.model_copy(update={"target_id": "00000000-0000-4000-8000-000000000000"})
    others = tuple(edge for edge in edges if edge.id != target.id)

    errors = _errors(plan, nodes, (dangling, *others))

    assert any("invalid parent reference" in error for error in errors)


def test_a_self_referential_chunk_order_edge_is_reported() -> None:
    plan, nodes, edges = _plan_and_records()
    order_edge = next(
        edge for edge in edges if edge.relationship_type in {"PREVIOUS_CHUNK", "NEXT_CHUNK"}
    )
    looped = order_edge.model_copy(update={"target_id": order_edge.source_id})
    others = tuple(edge for edge in edges if edge.id != order_edge.id)

    errors = _errors(plan, nodes, (looped, *others))

    assert any("invalid chunk order" in error for error in errors)
