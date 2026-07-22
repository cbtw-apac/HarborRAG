from __future__ import annotations

from harborrag_engine.ingestion.indexing import GraphMutationPlanner

from .indexing_helpers import (
    make_index_request,
    make_manifest,
    make_record,
    make_reference,
)


def relationship_types(plan) -> set[str]:
    return {edge.relationship_type for edge in plan.edges}


def test_jira_projection_uses_only_explicit_issue_metadata() -> None:
    references = (
        make_reference("logical-summary", "revision-summary", "hash-1", ordinal=0),
        make_reference("logical-comment", "revision-comment", "hash-2", ordinal=1),
    )
    metadata = {
        "source_kind": "jira",
        "issue_key": "HARBOR-7",
        "project_key": "HARBOR",
        "parent": {"key": "HARBOR-1"},
        "epic_key": "HARBOR-EPIC",
        "issue_links": ({"issue": {"key": "HARBOR-8"}},),
        "assignee": "Ada",
        "reporter": "Grace",
    }
    records = (
        make_record(
            references[0],
            artifact_revision_id="artifact-revision-1",
            role="jira.summary",
            metadata=metadata,
        ),
        make_record(
            references[1],
            artifact_revision_id="artifact-revision-1",
            role="jira.comment",
            metadata={**metadata, "comment_id": "comment-1"},
        ),
    )
    manifest = make_manifest(references, artifact_revision_id="artifact-revision-1")

    plan = GraphMutationPlanner().plan(make_index_request(proposed=manifest, records=records))

    assert {
        "HAS_ISSUE",
        "PARENT_ISSUE",
        "IN_EPIC",
        "LINKS_TO",
        "ASSIGNED_TO",
        "REPORTED_BY",
        "HAS_COMMENT",
    } <= relationship_types(plan)
    assert any("JiraIssue" in node.labels for node in plan.nodes)
    assert any("JiraComment" in node.labels for node in plan.nodes)


def test_graph_node_identities_are_artifact_scoped_for_shared_jira_entities() -> None:
    metadata = {
        "source_kind": "jira",
        "project_key": "HARBOR",
        "reporter": "Grace",
    }
    plans = []
    for artifact_id in ("jira://HARBOR/HARBOR-1", "jira://HARBOR/HARBOR-2"):
        reference = make_reference(
            f"logical-{artifact_id}",
            f"revision-{artifact_id}",
            f"hash-{artifact_id}",
            ordinal=0,
        )
        manifest = make_manifest(
            (reference,),
            artifact_revision_id=f"artifact-revision-{artifact_id}",
            artifact_id=artifact_id,
        )
        record = make_record(
            reference,
            artifact_revision_id=manifest.artifact_revision_id,
            artifact_id=artifact_id,
            role="jira.summary",
            metadata=metadata,
        )
        plans.append(
            GraphMutationPlanner().plan(
                make_index_request(proposed=manifest, records=(record,))
            )
        )

    first_ids = {str(node.id) for node in plans[0].nodes}
    second_ids = {str(node.id) for node in plans[1].nodes}
    assert first_ids.isdisjoint(second_ids)


def test_confluence_projection_preserves_space_parent_labels_links_and_sections() -> None:
    reference = make_reference("logical-1", "revision-1", "hash-1", ordinal=0)
    metadata = {
        "source_kind": "confluence",
        "space_key": "ENG",
        "page_id": "100",
        "ancestors": ({"id": "90"},),
        "labels": ("architecture", "rag"),
        "linked_page_ids": ("101",),
    }
    record = make_record(
        reference,
        artifact_revision_id="artifact-revision-1",
        structural_path=("Design",),
        metadata=metadata,
    )
    manifest = make_manifest((reference,), artifact_revision_id="artifact-revision-1")

    plan = GraphMutationPlanner().plan(make_index_request(proposed=manifest, records=(record,)))

    assert {
        "HAS_PAGE",
        "PARENT_PAGE",
        "HAS_LABEL",
        "LINKS_TO",
        "HAS_SECTION",
    } <= relationship_types(plan)
    assert any("ConfluencePage" in node.labels for node in plan.nodes)
    assert sum("ConfluenceLabel" in node.labels for node in plan.nodes) == 2


def test_pdf_projection_links_sections_to_pages_tables_and_figures() -> None:
    references = (
        make_reference("logical-table", "revision-table", "hash-1", ordinal=0),
        make_reference("logical-figure", "revision-figure", "hash-2", ordinal=1),
    )
    records = (
        make_record(
            references[0],
            artifact_revision_id="artifact-revision-1",
            role="table",
            structural_path=("Results",),
            metadata={"source_kind": "pdf", "table_id": "table-1"},
            page_start=3,
            page_end=3,
        ),
        make_record(
            references[1],
            artifact_revision_id="artifact-revision-1",
            role="figure",
            structural_path=("Results",),
            metadata={"source_kind": "pdf", "figure_id": "figure-1"},
            page_start=4,
            page_end=4,
        ),
    )
    manifest = make_manifest(references, artifact_revision_id="artifact-revision-1")

    plan = GraphMutationPlanner().plan(make_index_request(proposed=manifest, records=records))

    assert {"HAS_PAGE", "HAS_TABLE", "HAS_FIGURE"} <= relationship_types(plan)
    assert sum("Page" in node.labels for node in plan.nodes) == 2
    assert any("Table" in node.labels for node in plan.nodes)
    assert any("Figure" in node.labels for node in plan.nodes)
