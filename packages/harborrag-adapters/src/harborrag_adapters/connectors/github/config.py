"""Configuration for GitHub repository connectors."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field

from harborrag_adapters.connectors.file_filters import normalize_extension
from harborrag_adapters.connectors.policies.validation import (
    validate_http_tuning,
    validate_https_url,
    validate_non_negative_limit,
)

from .repository_paths import (
    DEFAULT_GITHUB_API_URL,
    DEFAULT_GITHUB_API_VERSION,
    DEFAULT_GITHUB_WEB_URL,
    GITHUB_BLOB_LIMIT_BYTES,
    parse_github_repository_url,
)


@dataclass(slots=True)
class GitHubRepositoryConfig:
    """Configuration for one GitHub repository source scope.

    Path filters and size limits apply during discovery and again on direct loads.
    ``token`` falls back to the ``GITHUB_TOKEN`` environment variable.

    Attributes:
        repository_url: HTTPS or SSH URL; supplies ``owner`` and ``repo`` when they are unset.
        ref: Branch, tag or commit to read; mutually exclusive with ``branch``.
        branch: Branch to read; mutually exclusive with ``ref``.
        commit_sha: Exact commit to read; mutually exclusive with ``ref`` and ``branch``.
        root_path: Repository subtree that bounds discovery and loading.
        process_file_callback: Called with path, size and MIME type; returns
            ``(should_process, reason)``.
        fail_on_error: Re-raise callback errors instead of skipping the file.
        allow_private_network: Allow ``api_url`` and ``web_url`` to target private hosts.
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
    allow_private_network: bool = False

    def __post_init__(self) -> None:
        """Resolve the repository URL and validate URLs, refs, limits and HTTP tuning.

        Raises:
            ValueError: If the repository is unidentified, refs conflict, or a value is invalid.
        """
        self.token = self.token or os.getenv("GITHUB_TOKEN")
        self.api_url = self.api_url.rstrip("/")
        self.web_url = self.web_url.rstrip("/")
        validate_https_url(
            "api_url", self.api_url, allow_private_network=self.allow_private_network
        )
        validate_https_url(
            "web_url", self.web_url, allow_private_network=self.allow_private_network
        )

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

        self.allowed_extensions = {normalize_extension(value) for value in self.allowed_extensions}
        self.excluded_extensions = {
            normalize_extension(value) for value in self.excluded_extensions
        }
