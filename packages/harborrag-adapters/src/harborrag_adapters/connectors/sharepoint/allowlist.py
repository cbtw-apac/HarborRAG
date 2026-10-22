"""Tenant, site, and path allowlist checks for SharePoint crawls."""

from __future__ import annotations

from typing import Any

from .config import SharePointSiteConfig
from .config_validation import normalize_identifier


class SharePointAllowlist:
    """Evaluate the configured allowlists; an unset allowlist allows everything."""

    def __init__(self, config: SharePointSiteConfig) -> None:
        """Bind the allowlists from a validated config."""
        self.config = config

    def rejected_tenant(self) -> str | None:
        """Return the tenant identifier when it is not allowlisted.

        A tenant allowlist with no known tenant ID fails closed: the connector
        cannot prove the content belongs to an approved tenant.
        """
        allowed = self.config.allowed_tenant_ids
        if allowed is None:
            return None
        tenant = normalize_identifier(self.config.tenant_id or "")
        if tenant and tenant in allowed:
            return None
        return self.config.tenant_id or "<unknown tenant>"

    def rejected_site(self, site: dict[str, Any]) -> str | None:
        """Return the site identifier when neither its ID nor URL is allowlisted.

        Graph site IDs look like ``hostname,siteCollectionId,webId``; an entry
        may name the full ID or just one of its GUID parts.
        """
        allowed = self.config.allowed_sites
        if allowed is None:
            return None
        site_id = str(site.get("id") or self.config.site_id or "")
        candidates = {site_id, *site_id.split(",")[1:]}
        for url in (site.get("webUrl"), self.config.site_url):
            if url:
                candidates.add(str(url))
        if any(normalize_identifier(value) in allowed for value in candidates if value):
            return None
        return str(site.get("webUrl") or site_id or "<unknown site>")

    def path_allowed(self, path: str) -> bool:
        """Return whether a drive-relative file path sits under an allowed prefix."""
        prefixes = self.config.allowed_path_prefixes
        if prefixes is None:
            return True
        normalized = path.strip("/").casefold()
        return any(_under(normalized, prefix) for prefix in prefixes)

    def folder_may_contain_allowed(self, folder_path: str) -> bool:
        """Return whether a folder is allowed or is an ancestor of an allowed prefix."""
        prefixes = self.config.allowed_path_prefixes
        if prefixes is None:
            return True
        normalized = folder_path.strip("/").casefold()
        return any(_under(normalized, prefix) or _under(prefix, normalized) for prefix in prefixes)


def _under(path: str, prefix: str) -> bool:
    return not prefix or path == prefix or path.startswith(f"{prefix}/")
