from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
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
        resolve_title: Callable[[str, str], str | None] | None = None,
    ) -> list[dict[str, object]]:
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
            # Confluence writes ac:link targets by title far more often than by id --
            # measured on a live space, 173 of 173 in-body page references carried
            # ri:content-title and none carried ri:content-id, so reading only the id
            # dropped every link a reader actually sees. A title is not an identity: the
            # graph keys pages by page_id, so a title-keyed node could never converge
            # with the real one. Resolve it to an id or record it as unresolved.
            title = str(attributes.get("ri:content-title") or "").strip()
            if not title:
                return
            resolved = self._resolve_title(space, title) if self._resolve_title else None
            if resolved and str(resolved).isdigit():
                self._add(space, str(resolved))
            else:
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
