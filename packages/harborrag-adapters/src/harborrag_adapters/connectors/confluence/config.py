"""Validated configuration for Confluence connector instances."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from harborrag_adapters.connectors.acl import validate_restricted_content_policy
from harborrag_adapters.connectors.attachments.processing import (
    DEFAULT_MAX_ATTACHMENT_SIZE_BYTES,
    CustomAttachmentParser,
    FileType,
)
from harborrag_adapters.connectors.policies.validation import (
    DEFAULT_MAX_NESTED_ITEMS,
    validate_http_tuning,
    validate_https_url,
    validate_non_negative_limit,
)

from .query import is_cloud_hostname, validate_token

_VALID_CONTENT_TYPES = {"page", "blogpost"}


class ConfluenceDeploymentType(StrEnum):
    """Supported Confluence deployments, selecting auth and API behavior."""

    CLOUD = "cloud"
    DATACENTER = "datacenter"


@dataclass(slots=True)
class ConfluenceSpaceConfig:
    """Configuration for one Confluence space source.

    Normalizes deployment type and credentials, and validates CQL filters, paging and attachment
    limits at construction.

    Attributes:
        token: API token; falls back to ``CONFLUENCE_TOKEN``.
        email: Account email; falls back to ``CONFLUENCE_EMAIL`` and is required on Cloud.
        deployment_type: Inferred from the host when ``None``.
        content_types: Content types to search; ``page`` and ``blogpost`` only.
        restricted_content: Policy applied to restricted pages.
        page_size: Search and child-page size, from 1 to 100.
    """

    space_key: str
    base_url: str
    token: str | None = field(default=None, repr=False)  # secret: keep out of repr/logs
    email: str | None = None
    deployment_type: ConfluenceDeploymentType | str | None = None
    content_types: list[str] = field(default_factory=lambda: ["page"])
    include_labels: list[str] = field(default_factory=list)
    exclude_labels: list[str] = field(default_factory=list)
    include_comments: bool = False
    include_attachments: bool = False
    custom_parsers: dict[FileType, CustomAttachmentParser] = field(default_factory=dict)
    process_attachment_callback: Callable[[str, int, str], tuple[bool, str]] | None = None
    max_attachment_size_bytes: int | None = DEFAULT_MAX_ATTACHMENT_SIZE_BYTES
    max_comments: int | None = DEFAULT_MAX_NESTED_ITEMS
    max_attachments: int | None = DEFAULT_MAX_NESTED_ITEMS
    fail_on_error: bool = False
    requests_per_minute: int = 60
    page_size: int = 25
    request_timeout_seconds: float = 30.0
    max_retries: int = 3
    backoff_factor: float = 0.5
    restricted_content: str = "skip"
    allow_private_network: bool = False

    def __post_init__(self) -> None:
        self.base_url = str(self.base_url).rstrip("/")
        validate_https_url(
            "base_url", self.base_url, allow_private_network=self.allow_private_network
        )
        self.space_key = validate_token(str(self.space_key).strip(), field_name="space key")
        self.restricted_content = validate_restricted_content_policy(self.restricted_content)
        self.token = self.token or os.getenv("CONFLUENCE_TOKEN")
        self.email = self.email or os.getenv("CONFLUENCE_EMAIL")

        if self.deployment_type is None:
            self.deployment_type = (
                ConfluenceDeploymentType.CLOUD
                if is_cloud_hostname(self.base_url)
                else ConfluenceDeploymentType.DATACENTER
            )
        elif isinstance(self.deployment_type, str):
            self.deployment_type = ConfluenceDeploymentType(self.deployment_type.lower())

        self.content_types = [content_type.lower() for content_type in self.content_types]
        invalid = sorted(set(self.content_types) - _VALID_CONTENT_TYPES)
        if invalid:
            raise ValueError(
                f"content_types must be one of {sorted(_VALID_CONTENT_TYPES)}, got {invalid}"
            )

        if self.deployment_type == ConfluenceDeploymentType.CLOUD and not self.email:
            raise ValueError("email is required for Confluence Cloud API token auth")
        if not self.token:
            raise ValueError("token is required for Confluence")
        validate_non_negative_limit(
            "max_attachment_size_bytes",
            self.max_attachment_size_bytes,
        )
        validate_non_negative_limit("max_comments", self.max_comments)
        validate_non_negative_limit("max_attachments", self.max_attachments)
        validate_http_tuning(
            requests_per_minute=self.requests_per_minute,
            request_timeout_seconds=self.request_timeout_seconds,
            max_retries=self.max_retries,
            backoff_factor=self.backoff_factor,
        )
        if not 1 <= self.page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")

    @property
    def deployment(self) -> ConfluenceDeploymentType:
        """Return the normalized deployment type."""
        if not isinstance(self.deployment_type, ConfluenceDeploymentType):
            raise RuntimeError("Confluence deployment type was not normalized")
        return self.deployment_type
