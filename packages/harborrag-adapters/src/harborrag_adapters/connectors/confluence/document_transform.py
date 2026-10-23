"""Confluence canonical document transformation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from html.parser import HTMLParser

from harborrag_core.domain import (
    Document,
    DocumentElement,
    DocumentRelation,
    ParsedDocument,
    RawDocument,
)

from .normalization import ConfluencePageNormalizer


class ConfluenceDocumentTransform:
    """Transform of Confluence page payloads into canonical documents with a comments section."""

    def __init__(self, pages: ConfluencePageNormalizer | None = None) -> None:
        """Bind the page normalizer.

        Args:
            pages: Page normalizer; ``ConfluencePageNormalizer`` when omitted.
        """
        self._pages = pages or ConfluencePageNormalizer()

    def transform(
        self,
        raw: RawDocument,
        parsed: ParsedDocument,
        document: Document,
    ) -> Document:
        """Normalize the raw page payload and append its comments as a ``Comments`` section.

        Structure comes from the Confluence normalizer; author, checksum, timestamps and tags come
        from the generic ``document`` provenance. Relations from both are merged.

        Args:
            raw: Raw document whose ``raw`` is the source payload mapping.
            parsed: Unused.
            document: Generic document supplying provenance and relations.

        Returns:
            Canonical document with page structure and comment paragraphs.

        Raises:
            ConfluenceNormalizationError: If no body parses or a table cannot be extracted.
            ValueError: If ``raw.raw`` is not the source payload mapping.
        """
        del parsed
        if not isinstance(raw.raw, Mapping):
            raise ValueError("Confluence canonical normalization requires the source payload")
        canonical = self._pages.normalize_payload(
            raw.raw,
            source_url=raw.source,
            default_space_key=str(raw.metadata.get("space_key") or ""),
        )
        comments = self._comment_elements(raw.metadata.get("comments"))
        provenance = replace(
            canonical.provenance,
            author=document.provenance.author,
            checksum=document.provenance.checksum,
            created_at=document.provenance.created_at,
            updated_at=document.provenance.updated_at,
            tags=document.provenance.tags,
            extra={
                **document.provenance.extra,
                **canonical.provenance.extra,
            },
        )
        return replace(
            canonical,
            content=[
                *canonical.content,
                *(
                    [
                        DocumentElement(
                            id=f"{raw.id}#comments",
                            type="heading",
                            content="Comments",
                            metadata={"level": 1},
                        )
                    ]
                    if comments
                    else []
                ),
                *comments,
            ],
            provenance=provenance,
            relations=self._merge_relations(
                canonical.relations,
                document.relations,
            ),
        )

    @classmethod
    def _comment_elements(cls, value: object) -> list[DocumentElement]:
        """Turn comment metadata into paragraph elements, skipping entries without an id or text."""
        if not isinstance(value, (list, tuple)):
            return []
        elements: list[DocumentElement] = []
        for item in value:
            if not isinstance(item, Mapping):
                continue
            comment_id = str(item.get("id") or "").strip()
            body = cls._visible_text(str(item.get("body") or ""))
            if not comment_id or not body:
                continue
            metadata = {
                "role": "confluence.comment",
                "comment_id": comment_id,
                "comment_kind": str(item.get("comment_kind") or "PAGE_COMMENT"),
            }
            for key in (
                "author",
                "created_at",
                "updated_at",
                "parent_comment_id",
                "status",
            ):
                item_value = item.get(key)
                if item_value is not None and str(item_value).strip():
                    metadata[key] = str(item_value)
            elements.append(
                DocumentElement(
                    id=f"confluence-comment:{comment_id}",
                    type="paragraph",
                    content=body,
                    metadata=metadata,
                )
            )
        return elements

    @staticmethod
    def _visible_text(value: str) -> str:
        """Strip comment HTML to whitespace-collapsed text, treating block tags as separators."""
        parser = _VisibleTextParser()
        parser.feed(value)
        parser.close()
        return " ".join("".join(parser.fragments).split())

    @staticmethod
    def _merge_relations(*groups: list[DocumentRelation]) -> list[DocumentRelation]:
        """Merge relation lists, keeping the first per (predicate, target id, target type)."""
        relations: dict[tuple[str, str, str], DocumentRelation] = {}
        for relation in (relation for group in groups for relation in group):
            key = (
                relation.predicate,
                relation.target_id,
                relation.target_type,
            )
            relations.setdefault(key, relation)
        return list(relations.values())


class _VisibleTextParser(HTMLParser):
    """HTML parser that collects text, inserting a space at block tag boundaries."""

    _BLOCK_TAGS = frozenset(
        {
            "blockquote",
            "br",
            "div",
            "li",
            "ol",
            "p",
            "pre",
            "table",
            "td",
            "th",
            "tr",
            "ul",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fragments: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del attrs
        if tag in self._BLOCK_TAGS:
            self.fragments.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCK_TAGS:
            self.fragments.append(" ")

    def handle_data(self, data: str) -> None:
        if data:
            self.fragments.append(data)
