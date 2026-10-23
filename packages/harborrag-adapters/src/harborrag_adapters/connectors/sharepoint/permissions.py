"""SharePoint drive-item permission classification against the document library.

Handles:
- Identity realms for Entra and SharePoint principals
- Classification of an item as inherited, restricted or unknown
- Cached permission reads per drive and item

Unreadable or empty permissions classify as ``unknown`` (fail closed); sharing links
are ignored.
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
    """Return ``entra:<tenant id>``. Raises ``ValueError`` for a blank tenant id."""

    tenant = str(tenant_id).strip()
    if not tenant:
        raise ValueError("an Entra realm needs a non-blank tenant id")
    return f"entra:{tenant}"


def sharepoint_realm(host: str) -> str:
    """Return ``sharepoint:<lowercase host>``. Raises ``ValueError`` for a blank host."""

    name = str(host).strip().lower()
    if not name:
        raise ValueError("a SharePoint realm needs a non-blank host")
    return f"sharepoint:{name}"


def site_realm(config: SharePointSiteConfig) -> str:
    """Return the identity realm for the principals a site's permissions name.

    ``entra:<tenant id>`` when a tenant id is configured, else ``sharepoint:<host>`` from
    ``hostname`` or the host at the start of ``site_id``. The ``sharepoint:`` realm
    matches no Entra principal.
    """

    tenant = str(config.tenant_id or "").strip()
    if tenant:
        return entra_realm(tenant)
    host = str(config.hostname or "").strip() or str(config.site_id or "").split(",", 1)[0]
    return sharepoint_realm(host)


def grant_ids(permissions: Sequence[Mapping[str, Any]]) -> frozenset[str]:
    """Return the ids of the non-link grants in a Graph permission list."""

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

    ``inherited`` only when every non-link grant has ``inheritedFrom`` and the grant ids
    equal the root's. Empty grants, an empty root or unidentifiable grantees give ``unknown``.

    Args:
        permissions: Graph permission list of the item; sharing links are ignored.
        root_grant_ids: Grant ids on the library root.
        realm: Identity realm of the principals the grants name.
        scope: Library scope, ``sharepoint-drive:<drive id>``.
        site_key: Site id; site-local principal ids are qualified as ``<site_key>/<id>``.

    Raises:
        ValueError: If ``site_key`` is blank.
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
    """Return the principals one permission names, preferring ``grantedToV2``.

    Site-local ids are qualified with ``site``; entries without an id are ignored.
    """
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
    """Resolver that reads Graph permissions for drive items and caches the results.

    Caches the library root grants per drive and item ACLs per ``(site, drive, item, cTag)``.
    Calls without a version are not cached; ``reset`` clears both caches at scan start.
    """

    def __init__(self, client: SharePointClient, *, realm: str) -> None:
        """Bind the Graph client and principal realm.

        Args:
            client: Graph client used for permission reads.
            realm: Identity realm of the principals.
        """
        self._client = client
        self._realm = realm
        self._root_grants: dict[str, frozenset[str]] = {}
        self._items: dict[tuple[str, str, str, str], SourceAcl] = {}

    def reset(self) -> None:
        """Clear the cached root grants and item ACLs."""

        # Permission changes rarely change an item's cTag, so a cache that outlived the
        # scan would keep serving an item after its access narrowed.
        self._root_grants.clear()
        self._items.clear()

    def item_acl(
        self, drive_id: str, item_id: str, *, site_key: str, version: str | None = None
    ) -> SourceAcl:
        """Return the item's ACL, or ``unknown`` when permissions cannot be read.

        A ``FetchError``, ``AuthorizationError`` or HTTP 403 gives ``unknown``.

        Args:
            drive_id: Drive id.
            item_id: Drive item id.
            site_key: Site id used to qualify site-local principals.
            version: Item ``cTag``; enables caching when set.

        Raises:
            AuthenticationError: If the credentials are rejected with a status other than 403.
        """

        key = (site_key, drive_id, item_id, version) if version else None
        scope = f"{_DRIVE_SCOPE_PREFIX}{drive_id}"
        if key is not None:
            cached = self._items.get(key)
            if cached is not None:
                return cached
        # TODO: URL-encode item ids in Graph endpoints.
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
        """Return the grant ids on the drive root, fetched once per drive until ``reset``."""
        cached = self._root_grants.get(drive_id)
        if cached is not None:
            return cached
        grants = grant_ids(self._permissions(f"drives/{drive_id}/root/permissions"))
        self._root_grants[drive_id] = grants
        return grants

    def _permissions(self, endpoint: str) -> list[dict[str, Any]]:
        """Return every permission on an endpoint, following ``@odata.nextLink`` pages.

        Raises:
            FetchError: If a page is malformed or the page cap is exceeded.
        """
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
