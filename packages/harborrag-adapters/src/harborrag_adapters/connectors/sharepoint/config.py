"""Validated configuration for SharePoint site connectors."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field

from harborrag_adapters.connectors.utils import (
    validate_http_tuning,
    validate_non_negative_limit,
)

from .utils import parse_sharepoint_site_url

DEFAULT_GRAPH_API_URL = "https://graph.microsoft.com/v1.0"
DEFAULT_MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024


@dataclass(slots=True)
class SharePointSiteConfig:
    """Configuration for one SharePoint document-library scope.

    A site can be addressed directly by ``site_id`` or parsed from ``site_url``.
    The connector then resolves a drive and walks drive items through Microsoft
    Graph.
    """

    site_url: str | None = None
    hostname: str | None = None
    site_path: str | None = None
    site_id: str | None = None
    drive_id: str | None = None
    drive_name: str | None = None
    root_path: str | None = None
    access_token: str | None = field(default=None, repr=False)  # secret
    tenant_id: str | None = None
    client_id: str | None = None
    client_secret: str | None = field(default=None, repr=False)  # secret
    graph_api_url: str = DEFAULT_GRAPH_API_URL
    allowed_extensions: set[str] = field(default_factory=set)
    excluded_extensions: set[str] = field(default_factory=set)
    include_hidden: bool = False
    process_file_callback: Callable[[str, int, str], tuple[bool, str]] | None = None
    max_file_size_bytes: int | None = DEFAULT_MAX_FILE_SIZE_BYTES
    fail_on_error: bool = False
    requests_per_minute: int = 120
    page_size: int = 200
    request_timeout_seconds: float = 60.0
    max_retries: int = 3
    backoff_factor: float = 0.5

    def __post_init__(self) -> None:
        """Resolve site/auth shortcuts and validate paging and size limits."""
        self.graph_api_url = self.graph_api_url.rstrip("/")
        self.access_token = self.access_token or os.getenv("MICROSOFT_GRAPH_TOKEN")
        self.tenant_id = self.tenant_id or os.getenv("MICROSOFT_TENANT_ID")
        self.client_id = self.client_id or os.getenv("MICROSOFT_CLIENT_ID")
        self.client_secret = self.client_secret or os.getenv("MICROSOFT_CLIENT_SECRET")

        if self.site_url and (not self.hostname or not self.site_path):
            hostname, site_path = parse_sharepoint_site_url(self.site_url)
            self.hostname = self.hostname or hostname
            self.site_path = self.site_path or site_path

        if not self.site_id and (not self.hostname or self.site_path is None):
            raise ValueError(
                "SharePoint config requires either site_id or site_url/hostname/site_path"
            )
        if not self.access_token and not (
            self.tenant_id and self.client_id and self.client_secret
        ):
            raise ValueError(
                "SharePoint config requires access_token or client credentials"
            )
        validate_http_tuning(
            requests_per_minute=self.requests_per_minute,
            request_timeout_seconds=self.request_timeout_seconds,
            max_retries=self.max_retries,
            backoff_factor=self.backoff_factor,
        )
        if not 1 <= self.page_size <= 999:
            raise ValueError("page_size must be between 1 and 999")
        validate_non_negative_limit("max_file_size_bytes", self.max_file_size_bytes)

        self.allowed_extensions = {
            _normalize_extension(value) for value in self.allowed_extensions
        }
        self.excluded_extensions = {
            _normalize_extension(value) for value in self.excluded_extensions
        }


def _normalize_extension(value: str) -> str:
    value = value.lower().strip()
    return value if value.startswith(".") else f".{value}"
