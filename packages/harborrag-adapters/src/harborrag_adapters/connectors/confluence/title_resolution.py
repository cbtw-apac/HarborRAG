"""Resolve Confluence ``ac:link`` page titles to page ids."""

from __future__ import annotations

import logging

from .content import ConfluenceContentAPI
from .query import quote_cql

logger = logging.getLogger("harborrag.adapters.connectors.confluence")


class PageTitleResolver:
    """Turn a page title into the page id the graph keys that page by.

    Confluence writes in-body link targets by title far more often than by id: measured on
    a live space, 173 of 173 ``ri:page`` references carried ``ri:content-title`` and none
    carried ``ri:content-id``. A title is not an identity -- the graph keys pages by
    ``page_id``, so emitting a title-keyed target would mint a second node that can never
    converge with the real page -- which is why the title has to become an id here or not
    become an edge at all.

    One request per distinct title per connector instance, negatives cached too: a title
    that belongs to another space costs one lookup for the run rather than one per
    occurrence. Only a *completed* search is cached -- see ``page_id_for_title``.
    """

    def __init__(self, content: ConfluenceContentAPI) -> None:
        self._content = content
        self._page_ids: dict[tuple[str, str], str | None] = {}

    def page_id_for_title(self, space_key: str, title: str) -> str | None:
        """Resolve one title, or return None when it cannot be resolved.

        A lookup failure never fails the document. The body still parses and the link is
        simply unresolved -- the same state every one of these links was in before titles
        were read at all -- so a rate limit or a revoked token degrades the graph rather
        than failing ingestion.

        A failed request is not a completed search, so it is never cached. Caching it
        would let one 429 or one dropped connection pin every later reference to that
        title as unresolved for the lifetime of the connector; a completed search that
        found nothing is a real answer and is cached as one.
        """

        cache_key = (space_key, title)
        if cache_key in self._page_ids:
            return self._page_ids[cache_key]
        try:
            resolved = self._lookup(space_key, title)
        except Exception as error:  # noqa: BLE001 - degrade to unresolved, never fail load
            logger.warning(
                "Confluence link title lookup failed space=%s title=%r error_type=%s",
                space_key,
                title,
                type(error).__name__,
            )
            return None
        if resolved is None:
            logger.info(
                "Confluence link title did not resolve to a page space=%s title=%r",
                space_key,
                title,
            )
        self._page_ids[cache_key] = resolved
        return resolved

    def _lookup(self, space_key: str, title: str) -> str | None:
        # ``type = page`` keeps a same-titled attachment, comment or blog post out of the
        # result set. An unconstrained CQL search returns every content type, the first
        # numeric id wins here, and every one of those types carries a numeric id -- so
        # without the constraint a relation can be keyed to the wrong source item.
        cql = f"space = {quote_cql(space_key)} and title = {quote_cql(title)} and type = page"
        for content in self._content.search(cql):
            candidate = str(content.get("id") or "")
            if candidate.isdigit():
                return candidate
        return None
