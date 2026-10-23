"""Page relations extracted from Confluence storage HTML.

Handles:
- ``links_to`` from page links and ``ri:page`` references
- ``includes`` from include and excerpt-include macros
- Title-only references resolved through a lookup callback
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

_SPACE_PAGE_PATH = re.compile(r"(?:^|/)spaces/(?P<space>[^/]+)/pages/(?P<page>[0-9]+)(?:/|$)")


class ConfluenceSourceRelationResolver:
    """Extractor of page link and include relations from Confluence storage HTML."""

    def relations(
        self,
        html: str,
        *,
        current_space: str,
        source_version: str,
        resolve_title: Callable[[str, str], str | None] | None = None,
    ) -> list[dict[str, object]]:
        """Return sorted ``links_to``/``includes`` relations for the pages referenced in ``html``.

        A title-only ``ri:page`` link that does not resolve yields no relation.

        Args:
            html: Confluence storage HTML.
            current_space: Space assumed for references that name none.
            source_version: Version recorded on every relation.
            resolve_title: Maps ``(space, title)`` to a page id, or None when unresolved.

        Returns:
            Relation dicts with predicate, target id, target type and version metadata.
        """
        parser = _ConfluenceReferenceParser(current_space, resolve_title=resolve_title)
        parser.feed(html)
        return [
            {
                "predicate": predicate,
                "target_id": target,
                "target_type": "document",
                "metadata": {
                    "source_relation_version": source_version,
                },
            }
            for predicate, target in sorted(parser.relations)
        ]

    def merge(
        self,
        existing: Sequence[Mapping[str, object]],
        *,
        html: str,
        current_space: str,
        source_version: str,
        resolve_title: Callable[[str, str], str | None] | None = None,
    ) -> list[dict[str, object]]:
        """Merge ``existing`` relations with those from ``html``, de-duplicated and sorted.

        Entries are keyed by predicate and target id; an extracted entry replaces an existing one.

        Args:
            existing: Relations already known for the page.
            html: Confluence storage HTML.
            current_space: Space assumed for references that name none.
            source_version: Version recorded on extracted relations.
            resolve_title: Maps ``(space, title)`` to a page id, or None when unresolved.

        Returns:
            Merged relation dicts sorted by predicate and target id.
        """
        values = [
            *(dict(value) for value in existing),
            *self.relations(
                html,
                current_space=current_space,
                source_version=source_version,
                resolve_title=resolve_title,
            ),
        ]
        unique = {
            (
                str(value.get("predicate")),
                str(value.get("target_id")),
            ): value
            for value in values
        }
        return [unique[key] for key in sorted(unique)]


class _ConfluenceReferenceParser(HTMLParser):
    """HTML parser collecting page references from ``ri:page`` elements and ``a`` links.

    References from ``ri:page`` and ``data-linked-resource-id`` inside an include or
    excerpt-include macro become ``includes``; ``href`` links are always ``links_to``.
    References without a space use ``current_space``.
    """

    def __init__(
        self,
        current_space: str,
        *,
        resolve_title: Callable[[str, str], str | None] | None = None,
    ) -> None:
        super().__init__(convert_charrefs=True)
        self._space = current_space
        self._resolve_title = resolve_title
        self._macro_stack: list[bool] = []
        self.relations: set[tuple[str, str]] = set()
        self.unresolved_titles: set[tuple[str, str]] = set()

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        if tag.casefold() == "ac:structured-macro":
            self._macro_stack.append(
                str(attributes.get("ac:name") or "").casefold() in {"include", "excerpt-include"}
            )
            return
        if tag.casefold() == "ri:page":
            space = str(attributes.get("ri:space-key") or self._space)
            page_id = attributes.get("ri:content-id")
            if page_id and str(page_id).isdigit():
                self._add(space, str(page_id))
                return
            title = str(attributes.get("ri:content-title") or "").strip()
            if not title:
                return
            resolved = self._resolve_title(space, title) if self._resolve_title else None
            if resolved and str(resolved).isdigit():
                self._add(space, str(resolved))
            else:
                # TODO: drop ``unresolved_titles``; nothing reads it.
                self.unresolved_titles.add((space, title))
            return
        if tag.casefold() == "a":
            linked_id = attributes.get("data-linked-resource-id")
            linked_space = str(attributes.get("data-linked-resource-space-key") or self._space)
            if linked_id and str(linked_id).isdigit():
                self._add(linked_space, str(linked_id))
                return
            href = attributes.get("href")
            if href:
                target = self._target_from_href(href)
                if target is not None:
                    self.relations.add(("links_to", target))

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "ac:structured-macro" and self._macro_stack:
            self._macro_stack.pop()

    def _add(self, space: str, page_id: str) -> None:
        """Record a page reference as ``includes`` inside an include macro, else ``links_to``."""
        predicate = "includes" if any(self._macro_stack) else "links_to"
        self.relations.add((predicate, f"confluence://{space}/{page_id}"))

    def _target_from_href(self, href: str) -> str | None:
        """Return the page id of a ``/spaces/<KEY>/pages/<id>`` or ``?pageId=`` link, else None."""
        parsed = urlsplit(href)
        match = _SPACE_PAGE_PATH.search(parsed.path)
        if match:
            return f"confluence://{match.group('space')}/{match.group('page')}"
        page_ids = parse_qs(parsed.query).get("pageId")
        if page_ids and page_ids[0].isdigit():
            return f"confluence://{self._space}/{page_ids[0]}"
        return None
