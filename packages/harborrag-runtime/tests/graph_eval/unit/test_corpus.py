from __future__ import annotations

import pytest

from harborrag_core.ingestion import GraphNodeRecord
from harborrag_engine.ingestion import GraphProjectionBatch

from ..corpus import CORPUS_SIGNATURES, EvalCorpus, build_corpus
from ..golden import PATH_CASES, STALENESS_CASES, SUBGRAPH_CASES, TRIPLET_CASES
from ..sources import eval_documents

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def _edges(batch: GraphProjectionBatch, relation_type: str) -> set[tuple[str, str]]:
    return {
        (relation.source_node_key, relation.target_node_key)
        for relation in batch.relations
        if relation.relation_type.value == relation_type
    }


def _entity_types(batch: GraphProjectionBatch) -> dict[str, str]:
    return {node.node_key: node.entity_type.value for node in batch.nodes}


def _node(batch: GraphProjectionBatch, node_key: str) -> GraphNodeRecord:
    return next(node for node in batch.nodes if node.node_key == node_key)


def _by_entity(batch: GraphProjectionBatch) -> dict[tuple[str, str], str]:
    return {(node.entity_type.value, node.logical_id): node.node_key for node in batch.nodes}


def test_corpus_projects_the_declared_topology(corpus: EvalCorpus) -> None:
    assert {"runbook", "architecture", "decisions", "incident"} <= set(corpus.batches)
    # Gold-by-construction: the runbook batch carries the links_to edge from the
    # runbook source item to the architecture source item.
    runbook = corpus.batches["runbook"]
    links = {
        (relation.source_node_key, relation.target_node_key)
        for relation in runbook.relations
        if relation.relation_type.value == "links_to" and relation.source_explicit
    }
    assert (
        corpus.source_item_key("runbook"),
        corpus.source_item_key("architecture"),
    ) in links
    # The unresolved link produced a placeholder target in the runbook batch.
    placeholders = [node for node in runbook.nodes if node.attributes.get("placeholder") is True]
    assert len(placeholders) == 1
    # Every document contributes chunks, a version node, and exactly one source item --
    # including the attachment and placeholder-heavy provider batches.
    for document_id in corpus.batches:
        assert corpus.chunk_keys(document_id)
        assert corpus.document_version_key(document_id)
        assert corpus.source_item_key(document_id)


def test_corpus_is_deterministic() -> None:
    first, second = build_corpus(), build_corpus()
    for document_id in first.batches:
        assert {n.node_key for n in first.batches[document_id].nodes} == {
            n.node_key for n in second.batches[document_id].nodes
        }
        assert {r.relation_id for r in first.batches[document_id].relations} == {
            r.relation_id for r in second.batches[document_id].relations
        }


def test_every_provider_set_reaches_its_own_projector(corpus: EvalCorpus) -> None:
    """No provider set silently falls through to ``GenericSourceProjector``."""

    expected = {
        "runbook": "local_file",
        "space-overview": "confluence_page",
        "team-handbook": "confluence_page",
        "handbook-pdf": "confluence_attachment",
        "HR-1": "jira_issue",
        "setup-guide": "github_file",
        "security-policy": "sharepoint_file",
    }
    for document_id, entity_type in expected.items():
        batch = corpus.batches[document_id]
        assert _node(batch, corpus.source_item_key(document_id)).entity_type.value == entity_type


def test_confluence_child_of_reverses_to_parent_of(corpus: EvalCorpus) -> None:
    parent = corpus.source_item_key("space-overview")
    child = corpus.source_item_key("team-handbook")
    edges = _edges(corpus.batches["team-handbook"], "parent_of")
    assert (parent, child) in edges, "child_of must project as parent (space-overview) -> child"
    assert (child, parent) not in edges


def test_confluence_attachment_resolves_and_attached_to_reverses(corpus: EvalCorpus) -> None:
    page = corpus.source_item_key("team-handbook")
    attachment = corpus.source_item_key("handbook-pdf")
    page_batch = corpus.batches["team-handbook"]
    assert (page, attachment) in _edges(page_batch, "has_attachment")
    assert _node(page_batch, attachment).attributes.get("placeholder") is None
    # The attachment's own batch carries the same edge twice: once from the Confluence
    # projector's parent_page_id branch (page -> attachment) and once from the reversed
    # `attached_to` predicate. `relation_entity_type` types the far end of any
    # HAS_ATTACHMENT edge as an attachment, so the reversed edge's source is an
    # attachment-typed team-handbook node rather than the page itself -- direction is
    # what this pins: handbook-pdf is the object of every has_attachment edge, never
    # the subject.
    attachment_batch = corpus.batches["handbook-pdf"]
    reversed_edges = _edges(attachment_batch, "has_attachment")
    assert {target for _, target in reversed_edges} == {attachment}
    assert (page, attachment) in reversed_edges
    # The attachment's own batch only knows its parent page as a placeholder stand-in and
    # the page's batch supplies the same node key concretely. `GraphProjectionState.node`
    # prefers the concrete record within one batch; across batches the adapter writes
    # placeholders with ON CREATE SET only, so the stand-in fills a gap but can never
    # downgrade the concretely-projected page.
    assert _node(attachment_batch, page).attributes["placeholder"] is True


def test_unmapped_includes_predicate_is_dropped_without_trace(corpus: EvalCorpus) -> None:
    """`includes` is reserved in RelationType and never projected -- pin that decision.

    A macro include is a real Confluence relation, so a future decision to map it (most
    likely onto LINKS_TO) has to be a reviewed change: today it produces no edge, no
    placeholder node, and no unresolved record at all.
    """

    assert "includes" in {
        relation.predicate for relation in eval_documents()["space-overview"].relations
    }
    batch = corpus.batches["space-overview"]
    assert not batch.unresolved_relations
    assert not [node for node in batch.nodes if node.attributes.get("placeholder") is True]
    assert corpus.source_item_key("team-handbook") not in {node.node_key for node in batch.nodes}


def test_confluence_comments_project_reply_to_and_section_links(corpus: EvalCorpus) -> None:
    batch = corpus.batches["team-handbook"]
    entities = _entity_types(batch)
    replies = _edges(batch, "reply_to")
    assert len(replies) == 1
    reply, parent = next(iter(replies))
    assert entities[reply] == entities[parent] == "comment"
    # The replying comment is the subject, the comment it answers is the object.
    assert _node(batch, reply).title == "Comment handbook-c2"
    assert _node(batch, parent).title == "Comment handbook-c1"
    section_links = {
        (source, target)
        for source, target in _edges(batch, "links_to")
        if entities.get(source) == "comment" and entities.get(target) == "section"
    }
    assert section_links


def test_confluence_table_is_contained_by_its_section(corpus: EvalCorpus) -> None:
    batch = corpus.batches["space-overview"]
    entities = _entity_types(batch)
    assert {
        (source, target)
        for source, target in _edges(batch, "contains")
        if entities.get(source) == "section" and entities.get(target) == "table"
    }


def test_jira_blocks_stay_canonical_through_the_reversal(corpus: EvalCorpus) -> None:
    blocker = corpus.source_item_key("HR-1")
    assert (blocker, corpus.source_item_key("HR-2")) in _edges(corpus.batches["HR-1"], "blocks")
    blocked = corpus.source_item_key("HR-3")
    edges = _edges(corpus.batches["HR-3"], "blocks")
    assert (blocker, blocked) in edges, "is_blocked_by must project as blocker -> blocked"
    assert (blocked, blocker) not in edges


def test_jira_duplicates_relates_to_and_issue_links(corpus: EvalCorpus) -> None:
    original = corpus.source_item_key("HR-2")
    duplicate = corpus.source_item_key("HR-4")
    assert (original, duplicate) in _edges(corpus.batches["HR-2"], "duplicates")
    edges = _edges(corpus.batches["HR-4"], "duplicates")
    assert (original, duplicate) in edges, "is_duplicated_by must project as original -> duplicate"
    assert (duplicate, original) not in edges
    assert (corpus.source_item_key("HR-5"), corpus.source_item_key("HR-1")) in _edges(
        corpus.batches["HR-5"], "relates_to"
    )
    assert (original, corpus.source_item_key("HR-5")) in _edges(corpus.batches["HR-2"], "links_to")


def test_jira_subtask_child_of_reverses_to_parent_of(corpus: EvalCorpus) -> None:
    parent = corpus.source_item_key("HR-1")
    subtask = corpus.source_item_key("HR-6")
    edges = _edges(corpus.batches["HR-6"], "parent_of")
    assert (parent, subtask) in edges
    assert (subtask, parent) not in edges


def test_github_contains_chain_pins_ref_and_commit(corpus: EvalCorpus) -> None:
    batch = corpus.batches["setup-guide"]
    keys = _by_entity(batch)
    file_key = corpus.source_item_key("setup-guide")
    assert keys[("github_file", "docs/guides/setup.md")] == file_key
    chain = (
        keys[("github_owner", "acme")],
        keys[("github_repository", "repo-1")],
        keys[("github_directory", "docs")],
        keys[("github_directory", "docs/guides")],
        file_key,
    )
    contains = _edges(batch, "contains")
    assert set(zip(chain[:-1], chain[1:], strict=True)) <= contains
    commit = keys[("github_commit", "9f1c0de5a2b34c7d")]
    assert (keys[("github_ref", "main")], commit) in _edges(batch, "points_to")
    assert (corpus.document_version_key("setup-guide"), commit) in _edges(batch, "resolved_at")


def test_sharepoint_contains_chain_runs_through_placeholder_folders(corpus: EvalCorpus) -> None:
    batch = corpus.batches["security-policy"]
    keys = _by_entity(batch)
    folders = (keys[("sharepoint_folder", "Policies")], keys[("sharepoint_folder", "folder-2")])
    chain = (
        keys[("sharepoint_site", "site-1")],
        keys[("sharepoint_drive", "drive-1")],
        *folders,
        corpus.source_item_key("security-policy"),
    )
    assert set(zip(chain[:-1], chain[1:], strict=True)) <= _edges(batch, "contains")
    # Folder nodes are census placeholders, not unresolved links: Task 11's placeholder
    # report has to split them out by entity_type.
    for folder in folders:
        assert _node(batch, folder).attributes["placeholder"] is True


def test_cross_source_link_never_resolves(corpus: EvalCorpus) -> None:
    batch = corpus.batches["HR-1"]
    placeholders = [node for node in batch.nodes if node.attributes.get("placeholder") is True]
    assert [node.logical_id for node in placeholders] == ["confluence://SPACE/team-handbook"]
    assert (corpus.source_item_key("HR-1"), placeholders[0].node_key) in _edges(batch, "links_to")
    # The placeholder is its own node: resolved_targets is per-run scope, so the real
    # Confluence page in the same corpus is never reached by a Jira link.
    assert placeholders[0].node_key != corpus.source_item_key("team-handbook")
    assert ("links_to", "confluence://SPACE/team-handbook") in {
        (relation.relation_type, relation.target_source_item_id)
        for relation in batch.unresolved_relations
    }


def test_every_golden_case_names_a_corpus_document(corpus: EvalCorpus) -> None:
    """`golden/` only runs live, so CI has to catch a case naming a dropped document.

    Importing the module also guards the engine result-model imports it depends on.
    """

    referenced = (
        {c.start_doc for c in PATH_CASES}
        | {c.end_doc for c in PATH_CASES}
        | {c.seed_doc for c in SUBGRAPH_CASES}
        | {c.subject_doc for c in TRIPLET_CASES}
        | {d for c in TRIPLET_CASES for d in c.expected_object_docs}
        | {c.seed_doc for c in STALENESS_CASES}
        | {d for c in STALENESS_CASES for d in c.stale_docs | c.forbidden_docs}
        | {d for c in SUBGRAPH_CASES for d in c.expected_docs | c.forbidden_docs}
    )
    assert referenced <= set(corpus.batches)


def test_corpus_exercises_full_signature_vocabulary(corpus: EvalCorpus) -> None:
    observed = {
        (kinds[r.source_node_key], r.relation_type.value, kinds[r.target_node_key])
        for batch in corpus.batches.values()
        for kinds in [{n.node_key: n.node_kind.value for n in batch.nodes}]
        for r in batch.relations
    }
    assert observed == CORPUS_SIGNATURES, (
        f"missing={sorted(CORPUS_SIGNATURES - observed)} extra={sorted(observed - CORPUS_SIGNATURES)}"
    )
