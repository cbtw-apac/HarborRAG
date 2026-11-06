"""Bounds and access requirements of an internal graph traversal."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from harborrag_core.retrieval import GraphAccessScope, GraphDirection, GraphTraversalQuery

_VALID = {
    "start_node_key": "node-1",
    "max_depth": 2,
    "max_nodes": 100,
    "direction": "both",
    "access_scope": None,
}


def test_traversal_query_carries_its_access_scope_out_of_serialized_form() -> None:
    scope = GraphAccessScope(document_ids=("document-1",))

    query = GraphTraversalQuery(**{**_VALID, "access_scope": scope})

    assert query.direction is GraphDirection.BOTH
    assert query.access_scope == scope
    assert "access_scope" not in query.model_dump()
    assert "document-1" not in repr(query)


def test_traversal_query_requires_the_caller_to_state_its_access_scope() -> None:
    arguments = dict(_VALID)
    del arguments["access_scope"]

    with pytest.raises(ValidationError, match="access_scope"):
        GraphTraversalQuery(**arguments)


@pytest.mark.parametrize(
    "override",
    [
        {"start_node_key": "  "},
        {"max_depth": 0},
        {"max_depth": 9},
        {"max_nodes": 0},
        {"max_nodes": 5_001},
        {"direction": "sideways"},
    ],
)
def test_traversal_query_rejects_out_of_bounds_walks(override: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        GraphTraversalQuery(**{**_VALID, **override})
