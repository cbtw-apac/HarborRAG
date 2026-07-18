"""Validated configuration for GitHub repository connectors."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field

from harborrag_adapters.connectors.utils.helpers import (
    validate_http_tuning,
    validate_non_negative_limit,
)

from .utils import (
    DEFAULT_GITHUB_API_URL,
    DEFAULT_GITHUB_API_VERSION,
    DEFAULT_GITHUB_WEB_URL,
    GITHUB_BLOB_LIMIT_BYTES,
    parse_github_repository_url,
)


@dataclass(slots=True)
class GitHubRepositoryConfig:
    """Configuration for one GitHub repository source scope.

    The connector reads one resolved ref from one repository. Path filters and
    size limits are applied during discovery and repeated during load for direct
    record access.
    """

    owner: str | None = None
    repo: str | None = None
    repository_url: str | None = None
    token: str | None = field(default=None, repr=False)  # secret: keep out of repr/logs
    ref: str | None = None
    branch: str | None = None
    commit_sha: str | None = None
    root_path: str | None = None
    api_url: str = DEFAULT_GITHUB_API_URL
    web_url: str = DEFAULT_GITHUB_WEB_URL
    api_version: str = DEFAULT_GITHUB_API_VERSION
    allowed_extensions: set[str] = field(default_factory=set)
    excluded_extensions: set[str] = field(default_factory=set)
    include_paths: list[str] = field(default_factory=list)
    exclude_paths: list[str] = field(default_factory=list)
    include_globs: list[str] = field(default_factory=list)
    exclude_globs: list[str] = field(default_factory=list)
    max_file_size_bytes: int | None = GITHUB_BLOB_LIMIT_BYTES
    process_file_callback: Callable[[str, int, str], tuple[bool, str]] | None = None
    fail_on_error: bool = False
    requests_per_minute: int = 120
    request_timeout_seconds: float = 30.0
    max_retries: int = 3
    backoff_factor: float = 0.5

    def __post_init__(self) -> None:
        """Resolve repository URL shortcuts and validate mutually exclusive refs."""
        self.token = self.token or os.getenv("GITHUB_TOKEN")
        self.api_url = self.api_url.rstrip("/")
        self.web_url = self.web_url.rstrip("/")

        if self.repository_url and (not self.owner or not self.repo):
            owner, repo = parse_github_repository_url(self.repository_url)
            self.owner = self.owner or owner
            self.repo = self.repo or repo

        if not self.owner or not self.repo:
            raise ValueError("GitHub config requires owner/repo or repository_url")
        if self.ref and self.branch:
            raise ValueError("GitHub config accepts ref or branch, not both")
        if self.commit_sha and (self.ref or self.branch):
            raise ValueError("GitHub config accepts commit_sha or ref/branch, not both")
        validate_non_negative_limit("max_file_size_bytes", self.max_file_size_bytes)
        validate_http_tuning(
            requests_per_minute=self.requests_per_minute,
            request_timeout_seconds=self.request_timeout_seconds,
            max_retries=self.max_retries,
            backoff_factor=self.backoff_factor,
        )

        self.allowed_extensions = {_normalize_extension(value) for value in self.allowed_extensions}
        self.excluded_extensions = {
            _normalize_extension(value) for value in self.excluded_extensions
        }


def _normalize_extension(value: str) -> str:
    value = value.lower().strip()
    return value if value.startswith(".") else f".{value}"
