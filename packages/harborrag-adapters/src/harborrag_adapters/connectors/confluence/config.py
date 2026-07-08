from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable

from .attachments import CustomAttachmentParser, FileType
from .detection import is_cloud_hostname

_VALID_CONTENT_TYPES = {"page", "blogpost", "comment"}


class ConfluenceDeploymentType(StrEnum):
    """Confluence deployment types."""

    CLOUD = "cloud"
    DATACENTER = "datacenter"


@dataclass(slots=True)
class ConfluenceSpaceConfig:
    """Configuration for a single Confluence space.

    Plain dataclass (not pydantic) to match the rest of the domain model in
    this repo, and because pydantic is not a declared dependency anywhere in
    this workspace.
    """

    space_key: str
    base_url: str
    token: str | None = None
    email: str | None = None
    deployment_type: ConfluenceDeploymentType | None = None
    content_types: list[str] = field(default_factory=lambda: ["page", "blogpost"])
    include_labels: list[str] = field(default_factory=list)
    exclude_labels: list[str] = field(default_factory=list)
    requests_per_minute: int = 60

    include_comments: bool = True
    include_attachments: bool = False
    custom_parsers: dict[FileType, CustomAttachmentParser] = field(default_factory=dict)
    process_attachment_callback: Callable[[str, int, str], tuple[bool, str]] | None = None
    max_attachment_size_bytes: int | None = None
    fail_on_error: bool = False

    def __post_init__(self) -> None:
        self.token = self.token or os.getenv("CONFLUENCE_TOKEN")
        self.email = self.email or os.getenv("CONFLUENCE_EMAIL")

        normalized_types = [t.lower() for t in self.content_types]
        invalid = [t for t in normalized_types if t not in _VALID_CONTENT_TYPES]
        if invalid:
            raise ValueError(
                f"Content type must be one of {sorted(_VALID_CONTENT_TYPES)}, got {invalid}"
            )
        self.content_types = normalized_types

        if not 1 <= self.requests_per_minute <= 1000:
            raise ValueError("requests_per_minute must be between 1 and 1000")

        if self.deployment_type is None:
            self.deployment_type = (
                ConfluenceDeploymentType.CLOUD
                if is_cloud_hostname(self.base_url)
                else ConfluenceDeploymentType.DATACENTER
            )
        elif isinstance(self.deployment_type, str):
            self.deployment_type = ConfluenceDeploymentType(self.deployment_type.lower())

        if self.deployment_type == ConfluenceDeploymentType.CLOUD:
            if not self.email:
                raise ValueError("Email is required for Confluence Cloud deployment")
            if not self.token:
                raise ValueError("API token is required for Confluence Cloud deployment")
        else:
            if not self.token:
                raise ValueError(
                    "Personal Access Token is required for Confluence Data Center/Server deployment"
                )