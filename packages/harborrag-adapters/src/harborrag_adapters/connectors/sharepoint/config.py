"""Validated configuration for SharePoint site connectors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from harborrag_adapters.connectors.file_filters import normalize_extension
from harborrag_adapters.connectors.limits import (
    DEFAULT_ALLOWED_EXTENSIONS as SHARED_ALLOWED_EXTENSIONS,
)
from harborrag_adapters.connectors.limits import DEFAULT_MAX_DOCUMENT_BYTES
from harborrag_adapters.connectors.policies.validation import (
    validate_http_tuning,
    validate_https_url,
    validate_non_negative_limit,
)

from ..environment import environment_default
from .config_validation import (
    normalize_allowlist,
    normalize_path_prefixes,
    validate_drive_path,
    validate_extensions,
    validate_positive_limit,
    validate_scope,
    validate_site_url,
)
from .delegated import default_token_cache_path
from .drive_paths import parse_sharepoint_site_url


class SharePointAuthMode(StrEnum):
    """Supported ways of obtaining Microsoft Graph tokens."""

    ACCESS_TOKEN = "access_token"
    CLIENT_CREDENTIALS = "client_credentials"
    DEVICE_CODE = "device_code"


# Fallbacks for fields a connection omits. Deployments set these per connection
# under `settings` in config/connectors.yaml; see config/connectors.example.yaml.
DEFAULT_GRAPH_API_URL = "https://graph.microsoft.com/v1.0"
DEFAULT_MAX_FILE_SIZE_BYTES = DEFAULT_MAX_DOCUMENT_BYTES
DEFAULT_ALLOWED_EXTENSIONS = frozenset(SHARED_ALLOWED_EXTENSIONS)


@dataclass(slots=True)
class SharePointSiteConfig:
    """Configuration for one SharePoint crawl scope.

    A site can be addressed directly by ``site_id`` or parsed from ``site_url``.
    The crawl scope is then one of:

    * the whole site: ``crawl_all_drives`` walks every document library;
    * one document library: the default drive, ``drive_id``, or ``drive_name``;
    * one folder: ``root_path`` inside that library, recursive unless
      ``recursive`` is off;
    * explicit files: ``item_ids`` inside that library.

    Allowlists (``allowed_tenant_ids``, ``allowed_sites``,
    ``allowed_path_prefixes``) are optional. When set, an item must pass every
    one of them before its content is downloaded.

    ``include_pages`` additionally discovers the site's modern pages. It is
    off by default so existing connections keep ingesting exactly the files
    they did before, and so a site owner opts in to page content explicitly.

    ``auth_mode`` selects how Graph tokens are obtained:

    * ``access_token``: a pre-issued token, used as-is;
    * ``client_credentials``: app-only, with ``client_secret``;
    * ``device_code``: delegated, as the user who signed in with the
      ``sharepoint.login`` command; its refresh token lives in
      ``token_cache_path``.

    When unset it is inferred from the credentials present, which never
    selects ``device_code``: delegated access must be chosen explicitly.
    """

    site_url: str | None = None
    hostname: str | None = None
    site_path: str | None = None
    site_id: str | None = None
    drive_id: str | None = None
    drive_name: str | None = None
    root_path: str | None = None
    item_ids: list[str] | None = None
    crawl_all_drives: bool = False
    recursive: bool = True
    allowed_tenant_ids: set[str] | None = None
    allowed_sites: set[str] | None = None
    allowed_path_prefixes: list[str] | None = None
    access_token: str | None = field(default=None, repr=False)  # secret
    tenant_id: str | None = None
    client_id: str | None = None
    client_secret: str | None = field(default=None, repr=False)  # secret
    auth_mode: SharePointAuthMode | str | None = None
    token_cache_path: str | None = None
    graph_api_url: str = DEFAULT_GRAPH_API_URL
    allowed_extensions: set[str] = field(default_factory=lambda: set(DEFAULT_ALLOWED_EXTENSIONS))
    excluded_extensions: set[str] = field(default_factory=set)
    include_hidden: bool = False
    include_pages: bool = False
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
        validate_https_url("graph_api_url", self.graph_api_url)
        self.tenant_id = self.tenant_id or environment_default("MICROSOFT_TENANT_ID")
        self.client_id = self.client_id or environment_default("MICROSOFT_CLIENT_ID")
        if self.auth_mode is not None:
            # Normalize first so " Device_Code" still takes the delegated guard below.
            self.auth_mode = str(self.auth_mode).strip().lower()
        if self.auth_mode != SharePointAuthMode.DEVICE_CODE:
            # A delegated connection must never fall back to ambient app credentials.
            self.access_token = self.access_token or environment_default("MICROSOFT_GRAPH_TOKEN")
            self.client_secret = self.client_secret or environment_default(
                "MICROSOFT_CLIENT_SECRET"
            )

        if self.site_url:
            validate_site_url(self.site_url)
        if self.site_url and (not self.hostname or not self.site_path):
            hostname, site_path = parse_sharepoint_site_url(self.site_url)
            self.hostname = self.hostname or hostname
            self.site_path = self.site_path or site_path

        if not self.site_id and (not self.hostname or self.site_path is None):
            raise ValueError(
                "SharePoint config requires either site_id or site_url/hostname/site_path"
            )
        self._resolve_auth_mode()
        validate_http_tuning(
            requests_per_minute=self.requests_per_minute,
            request_timeout_seconds=self.request_timeout_seconds,
            max_retries=self.max_retries,
            backoff_factor=self.backoff_factor,
        )
        if not 1 <= self.page_size <= 999:
            raise ValueError("page_size must be between 1 and 999")
        validate_non_negative_limit("max_file_size_bytes", self.max_file_size_bytes)
        validate_positive_limit("max_file_size_bytes", self.max_file_size_bytes)
        self.root_path = validate_drive_path("root_path", self.root_path)

        self.allowed_extensions = {normalize_extension(value) for value in self.allowed_extensions}
        self.excluded_extensions = {
            normalize_extension(value) for value in self.excluded_extensions
        }
        validate_extensions(self.allowed_extensions)
        self._normalize_scope_and_allowlists()

    def _resolve_auth_mode(self) -> None:
        if self.auth_mode is None:
            if self.access_token:
                self.auth_mode = SharePointAuthMode.ACCESS_TOKEN
            elif self.client_secret:
                self.auth_mode = SharePointAuthMode.CLIENT_CREDENTIALS
            else:
                raise ValueError("SharePoint config requires access_token or client credentials")
        try:
            self.auth_mode = SharePointAuthMode(str(self.auth_mode).strip().lower())
        except ValueError:
            modes = ", ".join(sorted(SharePointAuthMode))
            raise ValueError(f"auth_mode must be one of {modes}") from None

        if self.auth_mode == SharePointAuthMode.ACCESS_TOKEN and not self.access_token:
            raise ValueError("auth_mode access_token requires access_token")
        if self.auth_mode == SharePointAuthMode.CLIENT_CREDENTIALS and not (
            self.tenant_id and self.client_id and self.client_secret
        ):
            raise ValueError("SharePoint config requires access_token or client credentials")
        if self.auth_mode == SharePointAuthMode.DEVICE_CODE:
            if not (self.tenant_id and self.client_id):
                raise ValueError("auth_mode device_code requires tenant_id and client_id")
            if self.client_secret or self.access_token:
                raise ValueError("auth_mode device_code does not use client_secret or access_token")
            self.token_cache_path = str(
                Path(self.token_cache_path).expanduser()
                if self.token_cache_path
                else default_token_cache_path(self.tenant_id, self.client_id)
            )

    def _normalize_scope_and_allowlists(self) -> None:
        if self.item_ids is not None:
            self.item_ids = [str(value).strip() for value in self.item_ids if str(value).strip()]
        validate_scope(
            item_ids=self.item_ids,
            crawl_all_drives=self.crawl_all_drives,
            drive_selected=bool(self.drive_id or self.drive_name),
            root_path=self.root_path,
        )
        if self.allowed_tenant_ids is not None:
            self.allowed_tenant_ids = normalize_allowlist(
                "allowed_tenant_ids", self.allowed_tenant_ids, allow_urls=False
            )
        if self.allowed_sites is not None:
            self.allowed_sites = normalize_allowlist("allowed_sites", self.allowed_sites)
        if self.allowed_path_prefixes is not None:
            self.allowed_path_prefixes = normalize_path_prefixes(self.allowed_path_prefixes)
