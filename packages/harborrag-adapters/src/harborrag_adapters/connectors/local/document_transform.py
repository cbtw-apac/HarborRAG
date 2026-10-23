"""Document transform for local files."""

from __future__ import annotations

from dataclasses import replace

from harborrag_core.domain import Document, ParsedDocument, RawDocument


class LocalDocumentTransform:
    """Transform that uses a document's first level-one heading as its title."""

    def transform(
        self,
        raw: RawDocument,
        parsed: ParsedDocument,
        document: Document,
    ) -> Document:
        """Return the document titled by its first level-one heading, if it has one.

        A blank heading keeps the title; a heading without a level counts as level one.

        Args:
            raw: Unused.
            parsed: Unused.
            document: Canonical document to retitle.
        """
        del raw, parsed
        for element in document.content:
            if element.type != "heading" or element.metadata.get("level", 1) != 1:
                continue
            title = (element.content or "").strip()
            return replace(document, title=title or document.title)
        return document
