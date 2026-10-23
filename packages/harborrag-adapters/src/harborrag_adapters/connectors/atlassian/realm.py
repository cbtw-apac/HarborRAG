"""Identity realm of an Atlassian site, derived from its base URL."""

from __future__ import annotations

from urllib.parse import urlsplit


def atlassian_realm(base_url: str) -> str:
    """Return ``atlassian:<lowercase host>`` for a Jira or Confluence base URL.

    Jira and Confluence on one site share a realm; port, credentials and path are ignored.

    Args:
        base_url: Configured Jira or Confluence base URL.

    Returns:
        The realm string.

    Raises:
        ValueError: If the URL has no host.
    """

    try:
        host = urlsplit(base_url.strip()).hostname
    except ValueError:
        host = None
    if not host:
        raise ValueError(f"Atlassian base URL has no host: {base_url!r}")
    return f"atlassian:{host.lower()}"
