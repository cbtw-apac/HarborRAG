"""Confluence content search and nested-resource traversal."""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from typing import Any

from harborrag_adapters.connectors.exceptions import FetchError
from harborrag_adapters.connectors.policies.validation import truncate_with_limit
from harborrag_adapters.connectors.schemas import ConnectorQuery

from .client import ConfluenceClient
from .config import ConfluenceSpaceConfig
from .query import (
    CLOUD_CONTENT_EXPAND,
    COMMENT_EXPAND,
    CONTENT_EXPAND,
    LIGHT_EXPAND,
    build_search_params,
    extract_cursor,
    validate_content_id,
)


class ConfluenceContentAPI:
    """Confluence content traversal and child-resource pagination."""

    def __init__(self, client: ConfluenceClient, config: ConfluenceSpaceConfig) -> None:
        """Bind content traversal to a client and validated config."""
        self.client = client
        self.config = config

    def search(self, cql: str) -> Iterator[dict[str, Any]]:
        """Iterate CQL results across Cloud cursor and Data Center start paging."""
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            results, next_cursor = self.search_page(
                cql,
                cursor=cursor,
                limit=self.config.page_size,
            )
            yield from results
            if next_cursor is None:
                return
            if next_cursor in seen_cursors:
                raise FetchError("Confluence search pagination did not advance")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def search_page(
        self,
        cql: str,
        *,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Fetch exactly one provider page using a durable opaque cursor."""

        if limit < 1:
            raise ValueError("Confluence page limit must be positive")
        provider_limit = min(limit, self.config.page_size)
        cursor_token: str | None = None
        start = 0
        if cursor:
            kind, separator, value = cursor.partition(":")
            if not separator or kind not in {"cursor", "offset"}:
                raise ValueError("invalid Confluence discovery cursor")
            if len(value) > 4096 or any(ord(character) < 32 for character in value):
                raise ValueError("invalid Confluence discovery cursor")
            if kind == "cursor":
                cursor_token = value
            else:
                start = int(value)
                if start < 0:
                    raise ValueError("invalid Confluence discovery cursor")
        params = build_search_params(
            cql=cql,
            limit=provider_limit,
            start=start,
            cursor=cursor_token,
            expand=LIGHT_EXPAND,
        )
        response = self.client.get_json("content/search", params=params)
        results = list(response.get("results", []))
        next_url = response.get("_links", {}).get("next")
        next_token = extract_cursor(next_url)
        if next_token:
            return results, f"cursor:{next_token}"
        if cursor_token is not None:
            return results, None
        if _has_next_page(response, results, provider_limit):
            return results, f"offset:{start + len(results)}"
        return results, None

    def get_content_summary(self, content_id: str) -> dict[str, Any]:
        """Fetch lightweight content metadata for explicit-ID discovery."""
        content_id = validate_content_id(content_id)
        return self.client.get_json(
            f"content/{content_id}",
            params={"expand": LIGHT_EXPAND},
        )

    def get_content(self, content_id: str) -> dict[str, Any]:
        """Fetch the fully expanded page/blogpost needed for loading."""
        content_id = validate_content_id(content_id)
        return self.client.get_json(
            f"content/{content_id}",
            params={
                "expand": (
                    CLOUD_CONTENT_EXPAND
                    if self.config.deployment.value == "cloud"
                    else CONTENT_EXPAND
                )
            },
        )

    def fetch_comments(self, content_id: str) -> list[dict[str, Any]]:
        """Fetch comments for one content item, truncated to the configured cap."""
        content_id = validate_content_id(content_id)
        comments: list[dict[str, Any]] = []
        start = 0
        while True:
            response = self.client.get_json(
                f"content/{content_id}/child/comment",
                params={
                    "depth": "all",
                    "expand": COMMENT_EXPAND,
                    "limit": self.config.page_size,
                    "start": start,
                },
            )
            results = response.get("results", [])
            truncated = truncate_with_limit(comments, results, limit=self.config.max_comments)
            if truncated or not _has_next_page(response, results, self.config.page_size):
                return comments
            start += len(results)

    def list_attachments(self, content_id: str) -> list[dict[str, Any]]:
        """Fetch attachment metadata for one content item, truncated to the configured cap."""
        content_id = validate_content_id(content_id)
        attachments: list[dict[str, Any]] = []
        start = 0
        while True:
            response = self.client.get_json(
                f"content/{content_id}/child/attachment",
                params={"limit": self.config.page_size, "start": start},
            )
            results = response.get("results", [])
            truncated = truncate_with_limit(attachments, results, limit=self.config.max_attachments)
            if truncated or not _has_next_page(response, results, self.config.page_size):
                return attachments
            start += len(results)

    def with_children(
        self,
        content_ids: list[str],
        query: ConnectorQuery,
    ) -> Iterator[str]:
        """Yield requested content IDs and optionally traverse child pages."""
        include_children = bool(query.filters.get("include_children"))
        seen: set[str] = set()
        for raw_content_id in content_ids:
            content_id = validate_content_id(raw_content_id)
            if content_id in seen:
                continue
            seen.add(content_id)
            yield content_id
            if include_children:
                yield from self.child_page_ids(
                    content_id,
                    recursive=query.recursive,
                    seen=seen,
                )

    def child_page_ids(
        self,
        content_id: str,
        *,
        recursive: bool,
        seen: set[str],
    ) -> Iterator[str]:
        """Traverse child page IDs, truncated at max_child_pages.

        ``islice`` stops pulling from the underlying generator as soon as the
        cap is reached, so traversal never fetches pages beyond what's needed
        -- ``max_child_pages`` caps the discovery count by stopping, not by
        failing the whole traversal once exceeded.
        """
        ids = self._child_page_ids_unbounded(content_id, recursive=recursive, seen=seen)
        if self.config.max_child_pages is None:
            yield from ids
            return
        yield from itertools.islice(ids, self.config.max_child_pages)

    def _child_page_ids_unbounded(
        self,
        content_id: str,
        *,
        recursive: bool,
        seen: set[str],
    ) -> Iterator[str]:
        """Traverse child page IDs iteratively, with no count limit applied.

        An explicit stack replaces recursive generator delegation so a deep or
        broad page hierarchy cannot grow the Python call stack or raise
        RecursionError.
        """
        root_id = validate_content_id(content_id)
        stack = [root_id]
        while stack:
            current_id = stack.pop()
            start = 0
            while True:
                response = self.client.get_json(
                    f"content/{current_id}/child/page",
                    params={"limit": self.config.page_size, "start": start},
                )
                results = response.get("results", [])
                for child in results:
                    child_id = str(child.get("id") or "")
                    if not child_id:
                        continue
                    child_id = validate_content_id(child_id)
                    if child_id in seen:
                        continue
                    seen.add(child_id)
                    yield child_id
                    if recursive:
                        stack.append(child_id)
                if not _has_next_page(response, results, self.config.page_size):
                    break
                start += len(results)


def _has_next_page(
    response: dict[str, Any],
    results: list[dict[str, Any]],
    requested_limit: int,
) -> bool:
    """Honor provider paging metadata, including server-clamped limits."""
    if not results:
        return False
    next_url = response.get("_links", {}).get("next")
    if next_url:
        return True

    size = _integer(response.get("size"), default=len(results))
    start = _integer(response.get("start"), default=0)
    total = _integer(response.get("total"), default=None)
    if size is None:
        size = len(results)
    if start is None:
        start = 0
    if total is not None and start + size < total:
        return True

    page_limit = _integer(response.get("limit"), default=requested_limit)
    if page_limit is None:
        page_limit = requested_limit
    return page_limit > 0 and len(results) >= page_limit


def _integer(value: object, *, default: int | None) -> int | None:
    """Return integer pagination metadata without accepting booleans."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, str)):
        try:
            return int(value)
        except ValueError:
            return default
    return default
