"""The identity realm of an Atlassian site, derived from its configured base URL."""

from __future__ import annotations

from urllib.parse import urlsplit


def atlassian_realm(base_url: str) -> str:
    """Return ``atlassian:<lowercase host>`` for a Jira or Confluence base URL.

    Users and groups of one Atlassian site share a namespace, so the host names it;
    Jira and Confluence on the same site (``acme.atlassian.net`` and
    ``acme.atlassian.net/wiki``) share a realm. Port, credentials and path are not
    part of it. A URL without a host raises ``ValueError``.
    """

    try:
        host = urlsplit(base_url.strip()).hostname
    except ValueError:
        host = None
    if not host:
        raise ValueError(f"Atlassian base URL has no host: {base_url!r}")
    return f"atlassian:{host.lower()}"
