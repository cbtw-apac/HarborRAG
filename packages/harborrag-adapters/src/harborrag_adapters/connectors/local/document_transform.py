"""Local-file-owned canonical document transformation."""

from __future__ import annotations

from dataclasses import replace

from harborrag_core.domain import Document, ParsedDocument, RawDocument


class LocalDocumentTransform:
    """Use a document's first level-one heading as its display title."""

    def transform(
        self,
        raw: RawDocument,
        parsed: ParsedDocument,
        document: Document,
    ) -> Document:
        del raw, parsed
        for element in document.content:
            if element.type != "heading" or element.metadata.get("level", 1) != 1:
                continue
            title = (element.content or "").strip()
            return replace(document, title=title or document.title)
        return document
