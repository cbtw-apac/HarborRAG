"""Loader contract, and one assertion per shape the expanded samples were added for."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ..corpus import EvalCorpus
from ..sources import DEFAULTS_NAME, FIXTURES, eval_documents, load_document

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]

_DEFAULTS = {"source": "local_file", "content_type": "page", "extra": {"connector_type": "local"}}
_MINIMAL = {
    "id": "sample",
    "title": "Sample",
    "note": "why this sample exists",
    "elements": [{"id": "h1", "type": "heading", "content": "Sample", "metadata": {"level": 1}}],
}


def _write(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "sample.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _edges(corpus: EvalCorpus, document_id: str, relation_type: str) -> set[tuple[str, str]]:
    return {
        (relation.source_node_key, relation.target_node_key)
        for relation in corpus.batches[document_id].relations
        if relation.relation_type.value == relation_type
    }


def _entities(corpus: EvalCorpus, document_id: str) -> dict[tuple[str, str], str]:
    return {
        (node.entity_type.value, node.logical_id): node.node_key
        for node in corpus.batches[document_id].nodes
    }


def test_every_sample_declares_a_note() -> None:
    """A sample nobody can explain is a sample nobody can maintain."""

    for path in sorted(FIXTURES.glob("*/*.json")):
        if path.name == DEFAULTS_NAME:
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        note = payload.get("note", "")
        assert isinstance(note, str) and len(note) > 20, f"{path.name} needs a substantive note"


def test_defaults_are_layered_and_overridden_per_file() -> None:
    documents = eval_documents()
    # Inherited from confluence/_defaults.json, never restated per page.
    assert documents["team-handbook"].provenance.extra["space_key"] == "SPACE"
    assert documents["team-handbook"].content_type == "page"
    # The attachment overrides only content_type and keeps the directory's extra.
    attachment = documents["handbook-pdf"]
    assert attachment.content_type == "attachment"
    assert attachment.provenance.extra["space_key"] == "SPACE"
    assert attachment.provenance.extra["binding_kind"] == "ATTACHMENT"


def test_loader_rejects_a_malformed_envelope(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing required keys"):
        load_document(_write(tmp_path, {"id": "x", "title": "X"}), _DEFAULTS)
    with pytest.raises(ValueError, match="unknown element type"):
        load_document(
            _write(tmp_path, {**_MINIMAL, "elements": [{"id": "h1", "type": "headline"}]}),
            _DEFAULTS,
        )
    with pytest.raises(ValueError, match="relation missing"):
        load_document(
            _write(tmp_path, {**_MINIMAL, "relations": [{"predicate": "links_to"}]}), _DEFAULTS
        )
    with pytest.raises(ValueError, match="non-empty list"):
        load_document(_write(tmp_path, {**_MINIMAL, "elements": []}), _DEFAULTS)
    with pytest.raises(ValueError, match="invalid JSON"):
        path = tmp_path / "sample.json"
        path.write_text("{not json", encoding="utf-8")
        load_document(path, _DEFAULTS)


def test_loader_requires_a_source_and_content_type_somewhere(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be set here or in"):
        load_document(_write(tmp_path, _MINIMAL), {})


def test_github_file_without_ref_skips_the_commit_edges(corpus: EvalCorpus) -> None:
    """legacy-notes: the negative branch of `if ref_name and commit_sha`, plus a root file."""

    assert not _edges(corpus, "legacy-notes", "points_to")
    assert not _edges(corpus, "legacy-notes", "resolved_at")
    assert not [key for key in _entities(corpus, "legacy-notes") if key[0] == "github_directory"]
    # setup-guide still supplies both signatures, so the corpus vocabulary is unchanged.
    assert _edges(corpus, "setup-guide", "points_to")
    assert _edges(corpus, "setup-guide", "resolved_at")


def test_github_directory_node_is_shared_across_batches(corpus: EvalCorpus) -> None:
    """The whole contains spine depends on this: same directory, one node, two documents."""

    shared = ("github_directory", "docs/guides")
    assert _entities(corpus, "setup-guide")[shared] == _entities(corpus, "deploy-guide")[shared]


def test_github_deep_path_builds_one_directory_per_level(corpus: EvalCorpus) -> None:
    directories = sorted(
        logical_id
        for kind, logical_id in _entities(corpus, "deep-config")
        if kind == "github_directory"
    )
    assert directories == ["src", "src/main", "src/main/resources", "src/main/resources/config"]


def test_sharepoint_root_item_creates_no_folder_placeholders(corpus: EvalCorpus) -> None:
    """drive-readme: a parent ref with no id, so the drive contains the item directly."""

    assert not [key for key in _entities(corpus, "drive-readme") if key[0] == "sharepoint_folder"]


def test_sharepoint_leaf_folder_and_intermediate_folder_disagree_on_identity(
    corpus: EvalCorpus,
) -> None:
    """One real folder, two node keys -- pinned as behaviour, not endorsed as correct.

    ``SharePointSourceProjector`` keys only the *last* folder of a path by the item's
    parent id and every earlier one by accumulated path. security-policy sits directly in
    .../Policies/Security so that folder is `folder-2`; deep-audit sits two levels below
    it so the same folder is `Policies/Security`. Nothing reconciles the two, which is why
    the golden path case has them six hops apart rather than two.
    """

    shallow = _entities(corpus, "security-policy")
    deep = _entities(corpus, "deep-audit")
    assert ("sharepoint_folder", "folder-2") in shallow
    assert ("sharepoint_folder", "Policies/Security") in deep
    assert ("sharepoint_folder", "folder-2") not in deep
    # They do agree on every folder above the split.
    assert shallow[("sharepoint_folder", "Policies")] == deep[("sharepoint_folder", "Policies")]


def test_sharepoint_folder_node_is_shared_by_same_folder_items(corpus: EvalCorpus) -> None:
    leaf = ("sharepoint_folder", "folder-2")
    assert (
        _entities(corpus, "security-policy")[leaf] == _entities(corpus, "retention-schedule")[leaf]
    )


def test_long_body_splits_into_several_chunks(corpus: EvalCorpus) -> None:
    """onboarding is the only sample whose body passes the 60-token hard maximum."""

    assert len(corpus.chunk_keys("onboarding")) > len(corpus.chunk_keys("runbook"))


def test_confluence_reply_chain_is_transitive(corpus: EvalCorpus) -> None:
    """incident-log carries c1 <- c2 <- c3; team-handbook only proves a single pair."""

    replies = _edges(corpus, "incident-log", "reply_to")
    assert len(replies) == 2
    sources = {source for source, _ in replies}
    targets = {target for _, target in replies}
    # The middle comment is both a reply and the thing replied to.
    assert len(sources & targets) == 1


def test_jira_epic_chain_runs_two_parent_of_hops(corpus: EvalCorpus) -> None:
    epic = corpus.source_item_key("HR-7")
    story = corpus.source_item_key("HR-8")
    subtask = corpus.source_item_key("HR-9")
    assert (epic, story) in _edges(corpus, "HR-8", "parent_of")
    assert (story, subtask) in _edges(corpus, "HR-9", "parent_of")


def test_jira_issue_carries_four_distinct_link_predicates(corpus: EvalCorpus) -> None:
    """HR-10: the pinned issues each carry one predicate, so nothing covered a fan-out."""

    subject = corpus.source_item_key("HR-10")
    for relation_type, target in (
        ("blocks", "HR-9"),
        ("relates_to", "HR-7"),
        ("duplicates", "HR-8"),
        ("links_to", "HR-5"),
    ):
        assert (subject, corpus.source_item_key(target)) in _edges(corpus, "HR-10", relation_type)


def test_a_document_can_carry_more_than_one_placeholder(corpus: EvalCorpus) -> None:
    """The placeholder census counts per node, not per document."""

    batch = corpus.batches["changelog"]
    placeholders = {
        node.logical_id for node in batch.nodes if node.attributes.get("placeholder") is True
    }
    assert placeholders == {"retired-notes", "retired-plan"}
