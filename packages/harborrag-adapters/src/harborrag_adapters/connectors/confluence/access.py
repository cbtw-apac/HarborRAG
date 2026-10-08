"""Confluence read restrictions captured as source ACL evidence.

Confluence view restrictions are inherited: a reader must pass the page's own
read restriction *and* every restricted ancestor's. Each restricted level becomes
one ``SourceAcl`` level. Anything the payload cannot prove (missing block,
truncated list, entry without an id, unreadable ancestor) resolves to ``unknown``.
Space permissions are the connector's source scope and are governed by the
operator's source snapshot, not here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from harborrag_adapters.connectors.acl import SourceAcl, SourcePrincipal
from harborrag_adapters.connectors.exceptions import AuthorizationError, FetchError

from .content import ConfluenceContentAPI

_ID_FIELDS = {"user": ("accountId", "userKey", "username"), "group": ("id", "name")}


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def read_restriction_principals(
    holder: Mapping[str, Any],
) -> tuple[SourcePrincipal, ...] | None:
    """Return one item's read-restriction principals.

    ``()`` means the item adds no read restriction; ``None`` means the payload
    cannot prove that, which callers must treat as unresolved.
    """

    restrictions = _mapping(
        _mapping(_mapping(holder.get("restrictions")).get("read")).get("restrictions")
    )
    if "user" not in restrictions or "group" not in restrictions:
        return None
    principals: list[SourcePrincipal] = []
    for kind, id_fields in _ID_FIELDS.items():
        entries = _complete_results(_mapping(restrictions.get(kind)))
        if entries is None:
            return None
        for entry in entries:
            principal = _principal(kind, entry, id_fields)
            if principal is None:
                return None
            principals.append(principal)
    return tuple(principals)


def _complete_results(block: Mapping[str, Any]) -> list[Mapping[str, Any]] | None:
    results = block.get("results")
    if not isinstance(results, list) or _mapping(block.get("_links")).get("next"):
        return None
    limit, size = block.get("limit"), block.get("size")
    if isinstance(limit, int) and isinstance(size, int) and 0 < limit <= size:
        return None
    if any(not isinstance(entry, Mapping) for entry in results):
        return None
    return results


def _principal(
    kind: str, entry: Mapping[str, Any], id_fields: tuple[str, ...]
) -> SourcePrincipal | None:
    identifier = next(
        (str(entry[name]).strip() for name in id_fields if str(entry.get(name) or "").strip()),
        "",
    )
    if not identifier:
        return None
    display = str(entry.get("displayName") or entry.get("name") or "").strip() or None
    return SourcePrincipal(kind=kind, id=identifier, display_name=display)


class ConfluenceAccessResolver:
    """Combine a page's read restrictions with its ancestors', caching ancestors.

    Ancestor restrictions are not part of the page payload, so each ancestor is
    fetched once per scan: the connector calls ``reset`` when a scan starts, so a
    parent restricted after it was first seen is re-read by the next scan. A
    failed fetch is never cached. Concurrent describes may race on the cache; a
    duplicate fetch is harmless.
    """

    def __init__(self, content: ConfluenceContentAPI) -> None:
        self._content = content
        self._ancestors: dict[str, tuple[SourcePrincipal, ...] | None] = {}

    def reset(self) -> None:
        """Forget cached ancestor restrictions before a new scan."""

        self._ancestors.clear()

    def page_acl(self, content: Mapping[str, Any]) -> SourceAcl:
        own = read_restriction_principals(content)
        ancestors = content.get("ancestors")
        if own is None or not isinstance(ancestors, list):
            return SourceAcl.unknown("page_read_restrictions_unavailable")
        levels = [own] if own else []
        for ancestor in ancestors:
            level = self._ancestor_level(_mapping(ancestor))
            if level is None:
                return SourceAcl.unknown("ancestor_read_restrictions_unavailable")
            if level:
                levels.append(level)
        if not levels:
            return SourceAcl.inherited()
        return SourceAcl.restricted(levels, reason="confluence_read_restriction")

    def _ancestor_level(self, ancestor: Mapping[str, Any]) -> tuple[SourcePrincipal, ...] | None:
        if "restrictions" in ancestor:
            return read_restriction_principals(ancestor)
        ancestor_id = str(ancestor.get("id") or "").strip()
        if ancestor_id not in self._ancestors:
            try:
                payload: Mapping[str, Any] = self._content.get_read_restrictions(ancestor_id)
            except (FetchError, AuthorizationError, ValueError):
                return None
            self._ancestors[ancestor_id] = read_restriction_principals(payload)
        return self._ancestors[ancestor_id]
