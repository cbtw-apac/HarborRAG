from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

_SPACE_PAGE_PATH = re.compile(r"(?:^|/)spaces/(?P<space>[^/]+)/pages/(?P<page>[0-9]+)(?:/|$)")


class ConfluenceSourceRelationResolver:
    """Extract page links and include references from Confluence storage HTML."""

    def relations(
        self,
        html: str,
        *,
        current_space: str,
        source_version: str,
    ) -> list[dict[str, object]]:
        parser = _ConfluenceReferenceParser(current_space)
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
    ) -> list[dict[str, object]]:
        values = [
            *(dict(value) for value in existing),
            *self.relations(
                html,
                current_space=current_space,
                source_version=source_version,
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
    def __init__(self, current_space: str) -> None:
        super().__init__(convert_charrefs=True)
        self._space = current_space
        self._macro_stack: list[bool] = []
        self.relations: set[tuple[str, str]] = set()

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
            page_id = attributes.get("ri:content-id")
            if page_id and str(page_id).isdigit():
                space = str(attributes.get("ri:space-key") or self._space)
                self._add(space, str(page_id))
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
        predicate = "includes" if any(self._macro_stack) else "links_to"
        self.relations.add((predicate, f"confluence://{space}/{page_id}"))

    def _target_from_href(self, href: str) -> str | None:
        parsed = urlsplit(href)
        match = _SPACE_PAGE_PATH.search(parsed.path)
        if match:
            return f"confluence://{match.group('space')}/{match.group('page')}"
        page_ids = parse_qs(parsed.query).get("pageId")
        if page_ids and page_ids[0].isdigit():
            return f"confluence://{self._space}/{page_ids[0]}"
        return None
