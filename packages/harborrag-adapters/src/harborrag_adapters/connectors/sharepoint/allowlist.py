"""Tenant, site, and path allowlist checks for SharePoint crawls."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from urllib.parse import unquote, urlparse

from .config import SharePointSiteConfig
from .config_validation import normalize_identifier, path_match_key


class SharePointAllowlist:
    """Evaluate the configured allowlists; an unset allowlist allows everything.

    Args:
        config: Validated connector config that holds the allowlists.
    """

    def __init__(self, config: SharePointSiteConfig) -> None:
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

    def path_allowed(self, path: str, *, library_names: Sequence[str] = ()) -> bool:
        """Return whether a drive-relative file path sits under an allowed prefix.

        Args:
            path: Drive-relative path of the file.
            library_names: Names of the library that holds the file. A prefix
                that starts with one of them also matches the rest of its path.

        Returns:
            True when no path allowlist is set or the file is under a prefix.
        """
        prefixes = self._prefixes(library_names)
        if prefixes is None:
            return True
        key = path_match_key(path)
        return any(_under(key, prefix) for prefix in prefixes)

    def folder_may_contain_allowed(
        self, folder_path: str, *, library_names: Sequence[str] = ()
    ) -> bool:
        """Return whether a folder is allowed or is an ancestor of an allowed prefix.

        Args:
            folder_path: Drive-relative path of the folder.
            library_names: Names of the library that holds the folder, as for
                ``path_allowed``.

        Returns:
            True when no path allowlist is set or the folder may hold allowed files.
        """
        prefixes = self._prefixes(library_names)
        if prefixes is None:
            return True
        key = path_match_key(folder_path)
        return any(_under(key, prefix) or _under(prefix, key) for prefix in prefixes)

    def _prefixes(self, library_names: Sequence[str]) -> list[str] | None:
        """Return the configured prefixes, plus each one written from the library down.

        Prefixes are relative to the library, so ``Shared Documents/Q3 Plans`` in
        the library named ``Shared Documents`` also allows ``Q3 Plans``.
        """
        prefixes = self.config.allowed_path_prefixes
        if prefixes is None:
            return None
        names = {path_match_key(name) for name in library_names}
        inside_library = [
            prefix.partition("/")[2] for prefix in prefixes if prefix.partition("/")[0] in names
        ]
        return [*prefixes, *inside_library]


def library_names(drive: dict[str, Any]) -> tuple[str, ...]:
    """Return a document library's display name and the name in its web URL.

    A library shown as ``Documents`` usually lives at ``.../Shared%20Documents``,
    and operators write either name. Names the drive payload lacks are left out.
    """
    url_path = urlparse(str(drive.get("webUrl") or "")).path.rstrip("/")
    names = (str(drive.get("name") or ""), unquote(url_path.rpartition("/")[2]))
    return tuple(name for name in names if name.strip())


def _under(path: str, prefix: str) -> bool:
    return not prefix or path == prefix or path.startswith(f"{prefix}/")
