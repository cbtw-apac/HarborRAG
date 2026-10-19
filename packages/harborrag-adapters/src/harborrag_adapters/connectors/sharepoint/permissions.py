"""Classify SharePoint drive-item permissions against the document library.

The library (drive root) is the connector's source scope, governed by the
operator's source snapshot. An item is ``inherited`` only when every non-link
grant carries ``inheritedFrom`` *and* its grant ids equal the library root's:
comparing ids also catches a folder that broke inheritance and whose children
"inherit" a narrower set. Sharing links only widen access beyond the library,
which the library's snapshot already bounds, so they do not affect the result.

An empty grant list, an empty library root, grants naming no identifiable
principal, or a ``FetchError``, ``AuthorizationError`` or HTTP 403 while reading
permissions all give ``unknown`` (fail closed). The SharePoint client reports 403
as ``AuthenticationError(status_code=403)``; any other ``AuthenticationError``
(401, token failures) still propagates and aborts the run.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from harborrag_adapters.connectors.acl import SourceAcl, SourcePrincipal
from harborrag_adapters.connectors.exceptions import (
    AuthenticationError,
    AuthorizationError,
    FetchError,
)

from .client import SharePointClient
from .config import SharePointSiteConfig

_MAX_PERMISSION_PAGES = 50
_FORBIDDEN = 403
_MAX_CACHED_ITEMS = 100_000
_DRIVE_SCOPE_PREFIX = "sharepoint-drive:"
_GRANTEE_KINDS = (
    ("user", "user"),
    ("group", "group"),
    ("siteUser", "site_user"),
    ("siteGroup", "site_group"),
)
# Site users and groups are numbered per site collection, so their ids mean nothing
# outside the site they were read from; Entra users and groups are tenant-wide.
_SITE_LOCAL_KINDS = frozenset({"site_user", "site_group"})


def entra_realm(tenant_id: str) -> str:
    """Return ``entra:<tenant id>``, the identity realm of a Microsoft Entra tenant."""

    tenant = str(tenant_id).strip()
    if not tenant:
        raise ValueError("an Entra realm needs a non-blank tenant id")
    return f"entra:{tenant}"


def sharepoint_realm(host: str) -> str:
    """Return ``sharepoint:<lowercase host>``, a placeholder realm for a SharePoint site."""

    name = str(host).strip().lower()
    if not name:
        raise ValueError("a SharePoint realm needs a non-blank host")
    return f"sharepoint:{name}"


def site_realm(config: SharePointSiteConfig) -> str:
    """Choose the identity realm for the principals a site's permissions name.

    With a configured tenant id (``tenant_id`` or its ``MICROSOFT_TENANT_ID``
    fallback) the realm is ``entra:<tenant id>``, the namespace an Entra directory
    resolves. Token-only configs have no tenant id, so the realm falls back to
    ``sharepoint:<host>``: the host of the configured site (``hostname``, which
    ``site_url`` fills in), else the host a Graph ``site_id`` leads with
    (``contoso.sharepoint.com,<guid>,<guid>``).

    A ``sharepoint:`` realm is a placeholder that no Entra directory resolves, so
    its principals match nobody once SharePoint access is enforced (fail closed)
    until the connector is configured with a tenant id. It never grants anything.
    """

    tenant = str(config.tenant_id or "").strip()
    if tenant:
        return entra_realm(tenant)
    host = str(config.hostname or "").strip() or str(config.site_id or "").split(",", 1)[0]
    return sharepoint_realm(host)


def grant_ids(permissions: Sequence[Mapping[str, Any]]) -> frozenset[str]:
    """Ids of the non-link grants in a Graph permission list."""

    return frozenset(
        str(permission["id"])
        for permission in permissions
        if not permission.get("link") and str(permission.get("id") or "").strip()
    )


def classify_item_permissions(
    permissions: Sequence[Mapping[str, Any]],
    root_grant_ids: frozenset[str],
    *,
    realm: str,
    scope: str,
    site_key: str,
) -> SourceAcl:
    """Classify one item's Graph permissions relative to its library root grants.

    ``scope`` is the library (``sharepoint-drive:<drive id>``) the root grants
    belong to; ``realm`` is the namespace of the principals the grants name.
    ``site_key`` is the stable identity of the site the item was read from: the
    ids of site-local principals (``site_user``, ``site_group``) are only unique
    within one site collection, so they are qualified as ``<site_key>/<local id>``.
    Without that, ``site_group:3`` on two sites would be one principal. A blank
    ``site_key`` raises ``ValueError``.
    """

    site = site_key.strip()
    if not site:
        raise ValueError("a site key is required to qualify site-local principals")
    grants = [permission for permission in permissions if not permission.get("link")]
    if not root_grant_ids or not grants:
        return SourceAcl.unknown(reason="item_permissions_empty", realm=realm, scope=scope)
    if all(grant.get("inheritedFrom") for grant in grants) and grant_ids(grants) == root_grant_ids:
        return SourceAcl.inherited(realm=realm, scope=scope)
    principals = tuple(
        dict.fromkeys(p for grant in grants for p in _grantees(grant, realm=realm, site=site))
    )
    if not principals:
        return SourceAcl.unknown(reason="item_grantees_unreadable", realm=realm, scope=scope)
    return SourceAcl.restricted(
        realm=realm, scope=scope, levels=[principals], reason="sharepoint_item_permissions"
    )


def _grantees(permission: Mapping[str, Any], *, realm: str, site: str) -> list[SourcePrincipal]:
    identities = permission.get("grantedToV2") or permission.get("grantedTo")
    if not isinstance(identities, Mapping):
        return []
    principals: list[SourcePrincipal] = []
    for field_name, kind in _GRANTEE_KINDS:
        identity = identities.get(field_name)
        if isinstance(identity, Mapping) and str(identity.get("id") or "").strip():
            display = str(identity.get("displayName") or "").strip() or None
            identifier = str(identity["id"]).strip()
            if kind in _SITE_LOCAL_KINDS:
                identifier = f"{site}/{identifier}"
            principals.append(
                SourcePrincipal(realm=realm, kind=kind, id=identifier, display_name=display)
            )
    return principals


class SharePointPermissionResolver:
    """Read Graph permissions for drive items; cache the library root per drive.

    Item results are cached per ``(site, drive, item, cTag)``, because paged
    discovery replays the whole traversal once per page. The site is part of the
    key because site-local principals are qualified with it. Calls without a
    version (loads) are never cached. The connector calls ``reset`` when a new
    scan starts: permission changes rarely change an item's ``cTag``, so a cache
    that outlived the scan would keep serving an item after its access narrowed.
    """

    def __init__(self, client: SharePointClient, *, realm: str) -> None:
        self._client = client
        self._realm = realm
        self._root_grants: dict[str, frozenset[str]] = {}
        self._items: dict[tuple[str, str, str, str], SourceAcl] = {}

    def reset(self) -> None:
        """Forget cached library-root and item permissions before a new scan."""

        self._root_grants.clear()
        self._items.clear()

    def item_acl(
        self, drive_id: str, item_id: str, *, site_key: str, version: str | None = None
    ) -> SourceAcl:
        """Return the item's ACL, or ``unknown`` when Graph cannot tell us.

        ``site_key`` identifies the site the item was read from; see
        :func:`classify_item_permissions`.
        """

        key = (site_key, drive_id, item_id, version) if version else None
        scope = f"{_DRIVE_SCOPE_PREFIX}{drive_id}"
        if key is not None:
            cached = self._items.get(key)
            if cached is not None:
                return cached
        try:
            acl = classify_item_permissions(
                self._permissions(f"drives/{drive_id}/items/{item_id}/permissions"),
                self._root_grant_ids(drive_id),
                realm=self._realm,
                scope=scope,
                site_key=site_key,
            )
        except (FetchError, AuthorizationError):
            return self._unavailable(scope)
        except AuthenticationError as exc:
            if exc.status_code != _FORBIDDEN:
                raise
            return self._unavailable(scope)
        if key is not None:
            if len(self._items) >= _MAX_CACHED_ITEMS:
                self._items.clear()
            self._items[key] = acl
        return acl

    def _unavailable(self, scope: str) -> SourceAcl:
        return SourceAcl.unknown(
            reason="item_permissions_unavailable", realm=self._realm, scope=scope
        )

    def _root_grant_ids(self, drive_id: str) -> frozenset[str]:
        cached = self._root_grants.get(drive_id)
        if cached is not None:
            return cached
        grants = grant_ids(self._permissions(f"drives/{drive_id}/root/permissions"))
        self._root_grants[drive_id] = grants
        return grants

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
