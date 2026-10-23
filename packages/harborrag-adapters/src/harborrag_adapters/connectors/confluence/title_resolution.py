"""Resolution of Confluence ``ac:link`` page titles to page ids."""

from __future__ import annotations

import logging
from hashlib import sha256

from .content import ConfluenceContentAPI
from .query import quote_cql

logger = logging.getLogger("harborrag.adapters.connectors.confluence")


def _title_digest(title: str) -> str:
    """Return a short non-reversible digest of a title, keeping page titles out of logs."""

    return sha256(title.encode("utf-8")).hexdigest()[:12]


class PageTitleResolver:
    """Resolver turning a page title into the page id the graph keys that page by.

    Caches one answer per (space, title) for the connector's lifetime, including completed
    searches that found nothing; a failed request is never cached.
    """

    def __init__(self, content: ConfluenceContentAPI) -> None:
        self._content = content
        self._page_ids: dict[tuple[str, str], str | None] = {}

    def page_id_for_title(self, space_key: str, title: str) -> str | None:
        """Resolve one title to a page id.

        A lookup failure is logged and returns None, so a rate limit or revoked token degrades the
        links instead of failing the document.

        Args:
            space_key: Space to search.
            title: Exact page title.

        Returns:
            The page id, or None when the title is unresolved or the lookup failed.
        """

        cache_key = (space_key, title)
        if cache_key in self._page_ids:
            return self._page_ids[cache_key]
        try:
            resolved = self._lookup(space_key, title)
        except Exception as error:  # noqa: BLE001 - degrade to unresolved, never fail load
            logger.warning(
                "Confluence link title lookup failed space=%s title_digest=%s error_type=%s",
                space_key,
                _title_digest(title),
                type(error).__name__,
            )
            return None
        if resolved is None:
            logger.info(
                "Confluence link title did not resolve to a page space=%s title_digest=%s",
                space_key,
                _title_digest(title),
            )
        self._page_ids[cache_key] = resolved
        return resolved

    def _lookup(self, space_key: str, title: str) -> str | None:
        # ``type = page`` keeps a same-titled attachment, comment or blog post out of the
        # result set.
        cql = f"space = {quote_cql(space_key)} and title = {quote_cql(title)} and type = page"
        for content in self._content.search(cql):
            candidate = str(content.get("id") or "")
            if candidate.isdigit():
                return candidate
        return None
