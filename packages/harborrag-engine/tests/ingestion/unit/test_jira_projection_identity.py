"""A Jira parent/subtask placeholder must share the real issue's node identity.

Jira payloads carry both the numeric id and the issue key for related issues.
The real issue node is keyed by issue key (issue_key before issue_id), so
placeholders projected from ``parent``/``subtasks`` must prefer the key too —
otherwise the same issue lands as two nodes (ISSUES.md, projection defect 1).
"""

from __future__ import annotations

from typing import Any

from harborrag_core.domain.element import DocumentElement
from harborrag_core.ingestion import GraphEntityType
from harborrag_engine.ingestion import (
    GraphProjectionBatch,
    GraphProjectionBuilder,
    GraphProjectionInput,
)

from .chunking_helpers import make_document, make_profile, make_request, make_service

_PROJECT = {"connector_type": "jira", "project_id": "project-1", "project_key": "ENG"}


def _batch(extra: dict[str, Any]) -> GraphProjectionBatch:
    document = make_document(
        [
            DocumentElement("h1", "heading", "Operations", {"level": 1}),
            DocumentElement("p1", "paragraph", "Run the worker."),
        ],
        source="jira",
        extra={**_PROJECT, **extra},
    )
    chunks = (
        make_service(make_profile(target=40, maximum=60), create_route_chunks=True)
        .chunk(make_request(document))
        .chunks
    )
    return GraphProjectionBuilder().build(
        GraphProjectionInput(
            document=document,
            chunks=chunks,
            resolved_targets={},
            graph_projection_version="graph-v1",
        )
    )


def _issue_keys(batch: GraphProjectionBatch, *, placeholder: bool) -> set[str]:
    return {
        node.node_key
        for node in batch.nodes
        if node.entity_type == GraphEntityType.JIRA_ISSUE
        and (node.attributes.get("placeholder") is True) == placeholder
    }


def test_parent_and_subtask_placeholders_share_the_real_issue_identity() -> None:
    related = _batch(
        {
            "issue_key": "ENG-2",
            "issue_id": "10002",
            "parent": {"id": "10001", "key": "ENG-1", "summary": "Parent issue"},
            "subtasks": [{"id": "10003", "key": "ENG-3", "summary": "Child task"}],
        }
    )
    placeholders = _issue_keys(related, placeholder=True)
    assert len(placeholders) == 2

    parent = _batch({"issue_key": "ENG-1", "issue_id": "10001"})
    subtask = _batch({"issue_key": "ENG-3", "issue_id": "10003"})
    real = _issue_keys(parent, placeholder=False) | _issue_keys(subtask, placeholder=False)
    assert placeholders == real
