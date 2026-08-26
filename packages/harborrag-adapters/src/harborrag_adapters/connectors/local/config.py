"""Validated configuration for local filesystem connectors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from harborrag_adapters.connectors.policies.validation import validate_non_negative_limit

from .filesystem_paths import DEFAULT_EXCLUDED_DIR_NAMES, normalize_extension, resolve_path

ChecksumMode = Literal["none", "stat", "sha256"]
DEFAULT_MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024


@dataclass(slots=True)
class LocalFileConfig:
    """Configuration for one local file or directory source scope.

    The local connector treats this path as a trust boundary. Direct loads and
    discovery results must remain inside this scope, including when symlinks are
    explicitly enabled.
    """

    source_path: str | Path
    allowed_extensions: set[str] = field(default_factory=set)
    excluded_extensions: set[str] = field(default_factory=set)
    include_paths: list[str] = field(default_factory=list)
    exclude_paths: list[str] = field(default_factory=list)
    include_globs: list[str] = field(default_factory=list)
    exclude_globs: list[str] = field(default_factory=list)
    excluded_dir_names: set[str] = field(default_factory=lambda: set(DEFAULT_EXCLUDED_DIR_NAMES))
    include_hidden: bool = False
    follow_symlinks: bool = False
    max_depth: int | None = None
    max_file_size_bytes: int | None = DEFAULT_MAX_FILE_SIZE_BYTES
    checksum_mode: ChecksumMode = "stat"
    process_file_callback: Callable[[str, int, str], tuple[bool, str]] | None = None
    fail_on_error: bool = False

    def __post_init__(self) -> None:
        """Resolve paths and normalize filters before connector use."""
        self.source_path = resolve_path(self.source_path)
        if not self.source_path.exists():
            raise ValueError(f"Local source path does not exist: {self.source_path}")
        if not (self.source_path.is_file() or self.source_path.is_dir()):
            raise ValueError("Local source path must be a regular file or directory")
        validate_non_negative_limit("max_depth", self.max_depth)
        validate_non_negative_limit("max_file_size_bytes", self.max_file_size_bytes)
        if self.checksum_mode not in {"none", "stat", "sha256"}:
            raise ValueError("checksum_mode must be one of: none, stat, sha256")

        self.allowed_extensions = {normalize_extension(value) for value in self.allowed_extensions}
        self.excluded_extensions = {
            normalize_extension(value) for value in self.excluded_extensions
        }
        self.excluded_dir_names = {value.strip() for value in self.excluded_dir_names}
