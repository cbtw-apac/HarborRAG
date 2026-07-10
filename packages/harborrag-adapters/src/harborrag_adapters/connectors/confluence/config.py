"""Validated configuration for Confluence connector instances."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from harborrag_adapters.connectors.attachments import (
    DEFAULT_MAX_ATTACHMENT_SIZE_BYTES,
    CustomAttachmentParser,
    FileType,
)
from harborrag_adapters.connectors.utils import (
    DEFAULT_MAX_NESTED_ITEMS,
    validate_non_negative_limit,
)

from .utils import is_cloud_hostname

_VALID_CONTENT_TYPES = {"page", "blogpost"}


class ConfluenceDeploymentType(StrEnum):
    """Supported Confluence auth/API deployment modes."""

    CLOUD = "cloud"
    DATACENTER = "datacenter"


@dataclass(slots=True)
class ConfluenceSpaceConfig:
    """Configuration for one Confluence source scope.

    The config normalizes Cloud vs Data Center auth, CQL defaults, paging, and
    attachment safety limits before the connector starts making requests.
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
    process_attachment_callback: Callable[[str, int, str], tuple[bool, str]] | None = (
        None
    )
    max_attachment_size_bytes: int | None = DEFAULT_MAX_ATTACHMENT_SIZE_BYTES
    max_comments: int | None = DEFAULT_MAX_NESTED_ITEMS
    max_attachments: int | None = DEFAULT_MAX_NESTED_ITEMS
    fail_on_error: bool = False
    requests_per_minute: int = 60
    page_size: int = 25
    request_timeout_seconds: float = 30.0
    max_retries: int = 3
    backoff_factor: float = 0.5

    def __post_init__(self) -> None:
        """Normalize env-backed credentials and validate ingestion limits."""
        self.base_url = str(self.base_url).rstrip("/")
        self.token = self.token or os.getenv("CONFLUENCE_TOKEN")
        self.email = self.email or os.getenv("CONFLUENCE_EMAIL")

        if self.deployment_type is None:
            self.deployment_type = (
                ConfluenceDeploymentType.CLOUD
                if is_cloud_hostname(self.base_url)
                else ConfluenceDeploymentType.DATACENTER
            )
        elif isinstance(self.deployment_type, str):
            self.deployment_type = ConfluenceDeploymentType(
                self.deployment_type.lower()
            )

        self.content_types = [
            content_type.lower() for content_type in self.content_types
        ]
        invalid = sorted(set(self.content_types) - _VALID_CONTENT_TYPES)
        if invalid:
            raise ValueError(
                f"content_types must be one of {sorted(_VALID_CONTENT_TYPES)}, "
                f"got {invalid}"
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
        if not 1 <= self.requests_per_minute <= 6000:
            raise ValueError("requests_per_minute must be between 1 and 6000")
        if not 1 <= self.page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")

    @property
    def deployment(self) -> ConfluenceDeploymentType:
        """Return the normalized deployment type after validation."""
        if not isinstance(self.deployment_type, ConfluenceDeploymentType):
            raise RuntimeError("Confluence deployment type was not normalized")
        return self.deployment_type
