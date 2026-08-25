"""The two-axis invariant across the whole merged corpus, where it has teeth.

A single document's batch is homogeneous almost by accident: one document contributes one
chain, so each node it touches has one child. Heterogeneity only appears once many
documents merge onto the same container -- which is exactly what a real graph is, and why
the live Confluence space ended up holding 104 pages and 149 attachments under one edge
type while every per-document test passed.
"""

from __future__ import annotations

import pytest

from harborrag_core.ingestion import KnowledgeNodeKind

from ..corpus import EvalCorpus, build_corpus

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]

_ATTACHMENT_SUFFIX = "_attachment"


@pytest.fixture(scope="module")
def corpus() -> EvalCorpus:
    return build_corpus()


def _merged(corpus: EvalCorpus):
    nodes = {node.node_key: node for batch in corpus.batches.values() for node in batch.nodes}
    contains = {
        (relation.source_node_key, relation.target_node_key)
        for batch in corpus.batches.values()
        for relation in batch.relations
        if relation.relation_type.value == "contains"
    }
    return nodes, contains


def _label(node) -> str:
    return node.entity_type.value if node.entity_type else node.node_kind.value


def test_every_container_holds_exactly_one_entity_type(corpus: EvalCorpus) -> None:
    """The invariant, over all 30-odd documents at once."""

    nodes, contains = _merged(corpus)
    held: dict[str, set[str]] = {}
    for source, target in contains:
        kind = nodes[source].node_kind
        if kind is KnowledgeNodeKind.DOCUMENT_VERSION:
            continue  # structural projection: sections, tables, comments
        if kind is KnowledgeNodeKind.DATA_SOURCE:
            # This corpus puts all five connectors behind ONE data source, so it holds
            # five container types here. In production a data source is one connector's
            # scope digest and holds one -- which the per-connector engine test asserts,
            # projecting a single connector at a time. Not a real violation; an artifact
            # of the merge that makes the rest of this test meaningful.
            continue
        held.setdefault(source, set()).add(_label(nodes[target]))

    offenders = {_label(nodes[key]): sorted(kinds) for key, kinds in held.items() if len(kinds) > 1}
    assert not offenders, offenders


def test_no_container_anywhere_holds_an_attachment(corpus: EvalCorpus) -> None:
    """An attachment belongs to one document, never to a container's document set."""

    nodes, contains = _merged(corpus)
    held = {
        _label(nodes[target])
        for _, target in contains
        if _label(nodes[target]).endswith(_ATTACHMENT_SUFFIX)
    }
    assert held == set(), held


def test_every_attachment_is_owned_by_a_document(corpus: EvalCorpus) -> None:
    """Taking attachments off the membership axis must not strand them.

    Both ends are checked, not just the target set: HAS_ATTACHMENT is the one edge that
    reaches an attachment, so a source that is a space, a drive or another attachment
    would put it back on a container -- the very thing the split removes -- while still
    leaving every attachment "owned".
    """

    nodes, _ = _merged(corpus)
    edges = {
        (relation.source_node_key, relation.target_node_key)
        for batch in corpus.batches.values()
        for relation in batch.relations
        if relation.relation_type.value == "has_attachment"
    }
    documents = {corpus.source_item_key(document_id) for document_id in corpus.batches}
    attachments = {key for key, node in nodes.items() if _label(node).endswith(_ATTACHMENT_SUFFIX)}
    assert attachments, "corpus has no attachments to check"
    assert attachments <= {target for _, target in edges}, attachments - {
        target for _, target in edges
    }
    assert {target for _, target in edges} <= attachments
    holders = {source for source, _ in edges}
    assert holders <= documents - attachments, holders - (documents - attachments)


def test_counting_a_containers_documents_needs_no_type_filter(corpus: EvalCorpus) -> None:
    """The whole point: one hop off a container yields documents and nothing else.

    Written as the query shape a caller actually uses -- follow CONTAINS once from each
    container and take what comes back, with no entity_type predicate anywhere.
    """

    nodes, contains = _merged(corpus)
    containers = {
        source
        for source, _ in contains
        if nodes[source].node_kind is KnowledgeNodeKind.SOURCE_ENTITY
    }
    for container in containers:
        children = {_label(nodes[t]) for s, t in contains if s == container}
        assert len(children) == 1, (_label(nodes[container]), sorted(children))
