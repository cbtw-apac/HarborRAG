"""Atlassian host helpers shared by the Jira and Confluence connectors."""

from __future__ import annotations

from urllib.parse import urlparse


def is_cloud_hostname(base_url: str) -> bool:
    """Return whether a base URL's host ends with ``.atlassian.net``."""
    try:
        hostname = urlparse(str(base_url)).hostname
    except ValueError:
        return False
    return bool(hostname and hostname.endswith(".atlassian.net"))
