"""One edge per logical source link, however many documents declare it.

Most links are declared by both ends (Jira returns an issue link on each issue), and
every declaration used to become its own version-owned edge. These cases build the two
declaring documents and count the edge across *both* batches -- a per-batch count cannot
see a parallel copy owned by the other document.
"""

from __future__ import annotations

import pytest

from harborrag_core.chunking import RelationType
from harborrag_core.domain.document import DocumentRelation
from harborrag_core.ingestion import GraphEdgeRecord
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId
from harborrag_engine.ingestion.projections.graph.graph_models import GraphDocumentTarget

from .graph_convergence_helpers import project

_A = "jira://ENG/ENG-1"
_B = "jira://ENG/ENG-2"


def _target(source_item_id: str, *declared: tuple[str, str]) -> GraphDocumentTarget:
    return GraphDocumentTarget(
        source_item_id=source_item_id,
        document_id=DocumentId(f"doc-{source_item_id}"),
        document_version_id=DocumentVersionId(f"version-{source_item_id}"),
        source_scope_id="tenant-1",
        declared_relations=frozenset(declared),
    )


def _issue(source_item_id: str, predicate: str, target: str, *, resolved, **options):
    key = source_item_id.rsplit("/", 1)[-1]
    return project(
        "jira",
        {"project_id": "ENG", "project_key": "ENG", "issue_key": key},
        source_item_id=source_item_id,
        relations=[DocumentRelation(predicate=predicate, target_id=target, target_type="document")],
        resolved_targets=resolved,
        **options,
    )


def _source_links(*batches) -> list[GraphEdgeRecord]:
    return [
        edge
        for batch in batches
        for edge in batch.relations
        if edge.attributes.get("source_relation") is True
    ]


@pytest.mark.parametrize(
    ("outward", "inward", "relation_type"),
    [
        ("blocks", "is_blocked_by", RelationType.BLOCKS),
        ("duplicates", "is_duplicated_by", RelationType.DUPLICATES),
        ("relates_to", "relates_to", RelationType.RELATES_TO),
    ],
)
def test_a_link_both_ends_declare_is_one_edge(
    outward: str, inward: str, relation_type: RelationType
) -> None:
    first = _issue(_A, outward, _B, resolved={_B: _target(_B, (inward, _A))})
    second = _issue(_B, inward, _A, resolved={_A: _target(_A, (outward, _B))})

    links = [edge for edge in _source_links(first, second) if edge.relation_type is relation_type]

    assert len(links) == 1, [(edge.source_node_key, edge.target_node_key) for edge in links]
    # The outward declarer -- or, for a symmetric link, the smaller id -- owns it.
    assert links[0].document_version_id == first.manifest.document_version_id


def test_an_inverse_link_the_far_end_does_not_declare_keeps_its_edge() -> None:
    """The non-owner steps aside only for an owner that states the link back."""

    blocked = _issue(_B, "is_blocked_by", _A, resolved={_A: _target(_A)})

    links = _source_links(blocked)

    assert [edge.relation_type for edge in links] == [RelationType.BLOCKS]
    # Still blocker -> blocked, owned by the only declaring document.
    assert links[0].target_node_key != links[0].source_node_key


def test_has_attachment_and_parent_of_defer_to_the_far_ends_projection() -> None:
    """Attachments and subtasks draw their own edge from their own metadata."""

    for predicate, target in (
        ("has_attachment", f"{_A}/attachments/55"),
        ("parent_of", _B),
    ):
        batch = _issue(_A, predicate, target, resolved={target: _target(target)})

        assert _source_links(batch) == [], predicate
        assert batch.unresolved_relations == ()


@pytest.mark.parametrize("predicate", ["is_blocked_by", "relates_to", "has_attachment"])
def test_an_unresolved_link_points_at_an_external_stub_in_repair(predicate: str) -> None:
    target = "jira://RHR/RHR-2687"

    structural = _issue(_A, predicate, target, resolved={})
    repaired = _issue(_A, predicate, target, resolved={}, external_stubs=True)

    # Publication resolves nothing yet, so it must not mint stubs.
    assert _source_links(structural) == []
    links = _source_links(repaired)
    assert len(links) == 1
    stub_key = (
        links[0].source_node_key if predicate == "is_blocked_by" else links[0].target_node_key
    )
    stub = next(node for node in repaired.nodes if node.node_key == stub_key)
    # The id and nothing else: the target lives somewhere this source does not read.
    assert stub.attributes == {"placeholder": True, "external": True}
    assert stub.title == stub.logical_id == target.rsplit("/", 1)[-1]
    assert [(u.target_source_item_id, u.predicate) for u in repaired.unresolved_relations] == [
        (target, predicate)
    ]


def test_the_stub_key_ignores_the_declaring_scope() -> None:
    target = "jira://RHR/RHR-2687"
    in_hr = _issue(_A, "relates_to", target, resolved={}, external_stubs=True)
    in_ops = _issue(
        _B,
        "relates_to",
        target,
        resolved={},
        external_stubs=True,
        source_scope_id="other-scope",
    )

    stubs = {
        node.node_key: node
        for batch in (in_hr, in_ops)
        for node in batch.nodes
        if node.attributes.get("external") is True
    }

    scopes = {
        node.logical_id
        for batch in (in_hr, in_ops)
        for node in batch.nodes
        if node.node_kind.value == "DataSource"
    }
    assert len(scopes) == 2
    # One stub for both declarers, although they sit in different scopes...
    assert len(stubs) == 1
    assert {edge.target_node_key for edge in _source_links(in_hr, in_ops)} == set(stubs)
    # ...and its key is the scope-free identity, never a scoped source-entity key.
    assert next(iter(stubs)).startswith("graph-v2-external-source-entity:")


def test_a_parent_already_placed_by_the_projector_gets_no_second_stub() -> None:
    """An uningested parent is the subtask projector's placeholder, not a new stub."""

    child = project(
        "jira",
        {
            "project_id": "ENG",
            "project_key": "ENG",
            "issue_key": "ENG-2",
            "parent": {"id": "1", "key": "RHR-1", "summary": "Elsewhere"},
        },
        source_item_id=_B,
        relations=[
            DocumentRelation(
                predicate="child_of", target_id="jira://RHR/RHR-1", target_type="document"
            )
        ],
        resolved_targets={},
        external_stubs=True,
    )

    parents = [edge for edge in child.relations if edge.relation_type is RelationType.PARENT_OF]

    assert len(parents) == 1
    assert not any(node.attributes.get("external") for node in child.nodes)


def test_a_subtask_and_its_parent_draw_one_parent_of_edge() -> None:
    """The subtask's projector owns it; the parent's `parent_of` resolves and steps aside."""

    parent = project(
        "jira",
        {"project_id": "ENG", "project_key": "ENG", "issue_key": "ENG-1"},
        source_item_id=_A,
        relations=[DocumentRelation(predicate="parent_of", target_id=_B, target_type="document")],
        resolved_targets={_B: _target(_B)},
        external_stubs=True,
    )
    subtask = project(
        "jira",
        {
            "project_id": "ENG",
            "project_key": "ENG",
            "issue_key": "ENG-2",
            "parent": {"id": "1", "key": "ENG-1", "summary": "Parent"},
        },
        source_item_id=_B,
        relations=[DocumentRelation(predicate="child_of", target_id=_A, target_type="document")],
        resolved_targets={_A: _target(_A)},
        external_stubs=True,
    )

    edges = [
        edge
        for batch in (parent, subtask)
        for edge in batch.relations
        if edge.relation_type is RelationType.PARENT_OF
    ]

    assert len(edges) == 1
    assert edges[0].document_version_id == subtask.manifest.document_version_id


def test_every_file_at_a_commit_shares_one_ref_to_commit_edge() -> None:
    def file_batch(path: str):
        return project(
            "github",
            {
                "owner": "acme",
                "repository": "repo-1",
                "repository_id": "repo-1",
                "path": path,
                "ref": "main",
                "commit_sha": "9f1c0de5a2b34c7d",
            },
            source_item_id=f"github://acme/repo-1/{path}",
        )

    points_to = {
        edge.relation_id
        for batch in (file_batch("docs/a.md"), file_batch("docs/b.md"))
        for edge in batch.relations
        if edge.relation_type is RelationType.POINTS_TO
    }

    # MERGE keys on relation_id: one id is one edge in the graph.
    assert len(points_to) == 1
