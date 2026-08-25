"""Body-less but titled documents must still reach both projections.

The transform itself. Its effect on a real release -- the gate that used to drop these
pages, and the reindex path that re-chunks them from canonical -- is asserted in
``harborrag-runtime``'s ``test_title_content.py``, which drives the whole pipeline.
"""

from __future__ import annotations

import pytest

from harborrag_core.domain.document import Document
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.provenance import DocumentProvenance
from harborrag_core.ingestion import BindingKind
from harborrag_engine.ingestion import produces_evidence, with_title_as_content


def _document(title: str, elements: list[DocumentElement]) -> Document:
    return Document(
        id="doc-1",
        title=title,
        content=list(elements),
        content_type="confluence_page",
        provenance=DocumentProvenance(source="confluence"),
    )


def _fallback(document: Document, binding: BindingKind = BindingKind.ROOT) -> Document:
    return with_title_as_content(document, binding=binding)


# The capture-stage gate itself, not a copy of it: DocumentCaptureStages
# ._has_indexable_content delegates to this, so the tests cannot drift from production.
_indexable = produces_evidence


def test_a_titled_page_with_no_body_becomes_indexable() -> None:
    """The measured defect: 30 Confluence pages were dropped for having no body.

    A Confluence index page whose body is only a child-page macro normalizes to zero
    content elements. It was therefore dropped as unsupported -- no document version and
    so no graph node -- which removed exactly the section pages that hold the hierarchy
    together and left unnamed stubs in their place.
    """

    page = _document(
        "Quality Control Management",
        [DocumentElement("macro-1", "paragraph", None, {"macro": "children"})],
    )
    assert _indexable(page) is False

    result = _fallback(page)

    assert _indexable(result) is True
    titles = [
        element.content
        for element in result.content
        if element.metadata.get("role") == "document.title"
    ]
    assert titles == ["Quality Control Management"]
    # The original body is preserved after the synthesized heading.
    assert [element.id for element in result.content] == ["doc-1#title", "macro-1"]


def test_whitespace_only_body_still_gets_its_title() -> None:
    page = _document("Section Index", [DocumentElement("p1", "paragraph", "   ")])

    assert _indexable(_fallback(page)) is True


def test_a_title_that_is_only_the_document_id_is_not_content() -> None:
    """``DocumentNormalizer._title`` falls back to ``raw.id``, and ``Document.id`` is
    ``raw.id`` too, so an unguarded fallback would make every parsed document indexable
    and retire the unsupported decision by accident."""

    page = _document("doc-1", [DocumentElement("p1", "paragraph", None)])

    result = _fallback(page)

    assert result is page
    assert _indexable(result) is False


def test_a_document_with_neither_title_nor_body_stays_unsupported() -> None:
    page = _document("", [DocumentElement("p1", "paragraph", None)])

    result = _fallback(page)

    assert result is page
    assert _indexable(result) is False


def test_a_page_that_already_has_prose_is_untouched() -> None:
    page = _document("Runbook", [DocumentElement("p1", "paragraph", "Restart the worker.")])

    assert _fallback(page) is page


@pytest.mark.parametrize("binding", [BindingKind.ATTACHMENT, BindingKind.EMBEDDED])
def test_a_payload_of_another_document_keeps_todays_behaviour(binding: BindingKind) -> None:
    """An attachment's title is a filename, not a page name, and its parent already
    links it into the graph -- so a chunk holding nothing but ``diagram.png`` would be
    pure retrieval noise. Only self-standing source objects get the fallback."""

    binary = _document("diagram.png", [DocumentElement("p1", "paragraph", None)])

    assert with_title_as_content(binary, binding=binding) is binary


def test_a_page_built_only_from_headings_has_no_evidence() -> None:
    """Segmentation turns a heading into a section path and never into a unit, so a
    heading-only page reached BuildProjections and raised on an empty vector batch."""

    page = _document(
        "Quality Control Management",
        [DocumentElement("h1", "heading", "Sub-pages", {"level": 2})],
    )
    assert _indexable(page) is False

    result = _fallback(page)

    assert _indexable(result) is True
    # The heading survives as structure; the title becomes the evidence.
    assert [element.type for element in result.content] == ["paragraph", "heading"]
