"""Classify SharePoint drive-item permissions against the document library.

The library (drive root) is the connector's source scope, governed by the
operator's source snapshot. An item is ``inherited`` only when every non-link
grant carries ``inheritedFrom`` *and* its grant ids equal the library root's:
comparing ids also catches a folder that broke inheritance and whose children
"inherit" a narrower set. Sharing links only widen access beyond the library,
which the library's snapshot already bounds, so they do not affect the result.

An empty grant list, an empty library root, grants naming no identifiable
principal, or a ``FetchError``/``AuthorizationError`` while reading permissions
all give ``unknown`` (fail closed). ``AuthenticationError`` still propagates.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from harborrag_adapters.connectors.acl import SourceAcl, SourcePrincipal
from harborrag_adapters.connectors.exceptions import AuthorizationError, FetchError

from .client import SharePointClient

_MAX_PERMISSION_PAGES = 50
_MAX_CACHED_ITEMS = 100_000
_GRANTEE_KINDS = (
    ("user", "user"),
    ("group", "group"),
    ("siteUser", "site_user"),
    ("siteGroup", "site_group"),
)


def grant_ids(permissions: Sequence[Mapping[str, Any]]) -> frozenset[str]:
    """Ids of the non-link grants in a Graph permission list."""

    return frozenset(
        str(permission["id"])
        for permission in permissions
        if not permission.get("link") and str(permission.get("id") or "").strip()
    )


def classify_item_permissions(
    permissions: Sequence[Mapping[str, Any]], root_grant_ids: frozenset[str]
) -> SourceAcl:
    """Classify one item's Graph permissions relative to its library root grants."""

    grants = [permission for permission in permissions if not permission.get("link")]
    if not root_grant_ids or not grants:
        return SourceAcl.unknown("item_permissions_empty")
    if all(grant.get("inheritedFrom") for grant in grants) and grant_ids(grants) == root_grant_ids:
        return SourceAcl.inherited()
    principals = tuple(dict.fromkeys(p for grant in grants for p in _grantees(grant)))
    if not principals:
        return SourceAcl.unknown("item_grantees_unreadable")
    return SourceAcl.restricted([principals], reason="sharepoint_item_permissions")


def _grantees(permission: Mapping[str, Any]) -> list[SourcePrincipal]:
    identities = permission.get("grantedToV2") or permission.get("grantedTo")
    if not isinstance(identities, Mapping):
        return []
    principals: list[SourcePrincipal] = []
    for field_name, kind in _GRANTEE_KINDS:
        identity = identities.get(field_name)
        if isinstance(identity, Mapping) and str(identity.get("id") or "").strip():
            display = str(identity.get("displayName") or "").strip() or None
            principals.append(SourcePrincipal(kind, str(identity["id"]).strip(), display))
    return principals


class SharePointPermissionResolver:
    """Read Graph permissions for drive items; cache the library root per drive.

    Item results are cached per ``(drive, item, cTag)`` for the resolver's
    lifetime, because the base ``discover_page`` fallback replays ``discover()``
    once per page. Calls without a version (loads) are never cached.
    """

    def __init__(self, client: SharePointClient) -> None:
        self._client = client
        self._root_grants: dict[str, frozenset[str]] = {}
        self._items: dict[tuple[str, str, str], SourceAcl] = {}

    def item_acl(self, drive_id: str, item_id: str, *, version: str | None = None) -> SourceAcl:
        """Return the item's ACL, or ``unknown`` when Graph cannot tell us."""

        key = (drive_id, item_id, version) if version else None
        if key is not None and key in self._items:
            return self._items[key]
        try:
            acl = classify_item_permissions(
                self._permissions(f"drives/{drive_id}/items/{item_id}/permissions"),
                self._root_grant_ids(drive_id),
            )
        except (FetchError, AuthorizationError):
            return SourceAcl.unknown("item_permissions_unavailable")
        if key is not None:
            if len(self._items) >= _MAX_CACHED_ITEMS:
                self._items.clear()
            self._items[key] = acl
        return acl

    def _root_grant_ids(self, drive_id: str) -> frozenset[str]:
        if drive_id not in self._root_grants:
            self._root_grants[drive_id] = grant_ids(
                self._permissions(f"drives/{drive_id}/root/permissions")
            )
        return self._root_grants[drive_id]

    def _permissions(self, endpoint: str) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        next_endpoint = endpoint
        for _ in range(_MAX_PERMISSION_PAGES):
            response = self._client.get_json(next_endpoint)
            page = response.get("value")
            if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
                raise FetchError("SharePoint permissions response contained an invalid value list")
            values.extend(page)
            next_link = response.get("@odata.nextLink")
            if not next_link:
                return values
            next_endpoint = str(next_link)
        raise FetchError("SharePoint permissions pagination exceeded its page cap")
