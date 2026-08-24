"""Title fallback for documents whose body normalizes to nothing."""

from __future__ import annotations

from dataclasses import replace

from harborrag_core.domain.document import Document
from harborrag_core.domain.element import DocumentElement
from harborrag_core.ingestion import BindingKind
from harborrag_engine.ingestion import produces_evidence

_SELF_STANDING = frozenset({BindingKind.ROOT})


def with_title_as_content(document: Document, *, binding: BindingKind) -> Document:
    """Give a body-less but titled document its title as content.

    A Confluence page whose body is only a child-page macro normalizes to zero content
    elements, so it was dropped as unsupported: no document, no version, and therefore
    no graph node. Those are exactly the section and index pages that hold a space
    together, so the hierarchy lost its interior and surfaced unnamed stubs instead.
    Such a page earns both projections -- its title is a real retrieval target, and it
    is a real parent in the tree.

    The emptiness test is ``produces_evidence``, not "has any content at all": a page
    built only from headings segments to zero evidence units, so it would reach
    ``BuildProjections`` and raise on an empty vector batch. Giving it the same title
    paragraph is what makes it publishable *and* retrievable, where the chunker's
    document-only route would leave it out of the evidence index entirely.

    Two guards keep this from swallowing the unsupported decision whole:

    ``DocumentNormalizer._title`` falls back to the document id, so ``title`` is never
    empty and an unguarded fallback would make *every* parsed document indexable --
    retiring ``_has_indexable_content`` by accident. ``title == document.id`` is that
    fallback exactly (both come from ``raw.id``), and an id is not a retrieval target.

    A binding that is a payload of some parent -- an attachment, an embedded image --
    is titled with a filename rather than a page name, and its parent already links it
    into the graph, so a chunk holding nothing but ``diagram.png`` buys retrieval
    nothing. Only a ``ROOT`` binding gets the fallback: that is the whole measured
    population, and ``CONTAINED`` has no producer in the tree to reason about yet.
    """

    if binding not in _SELF_STANDING:
        return document
    if produces_evidence(document):
        return document
    title = (document.title or "").strip()
    if not title or title == document.id:
        return document
    return replace(
        document,
        content=[
            DocumentElement(
                id=f"{document.id}#title",
                type="paragraph",
                content=title,
                metadata={"role": "document.title"},
            ),
            *document.content,
        ],
    )
