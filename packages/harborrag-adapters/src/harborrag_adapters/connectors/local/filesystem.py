"""Local filesystem boundary for traversal, selection and secure reads.

Handles:
- Directory walking with symlink, hidden-file, depth and cycle rules
- Source scope enforcement
- Descriptor-bound snapshot reads with a byte cap
- Record building and change-detection checksums
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from hashlib import sha256
from pathlib import Path

from harborrag_adapters.connectors.exceptions import DocumentProcessingError, FetchError
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_core.domain.source import SourceRecord

from .config import LocalFileConfig
from .filesystem_paths import (
    SUPPORTS_DIR_FD,
    is_hidden_path,
    resolve_path,
    stat_signature,
)
from .filters import file_paths_from_query
from .mappers import build_source_record
from .secure_read import LocalFileSnapshot, SecureReadScope, read_snapshot_beneath
from .selection import LocalFileSelector
from .skips import LocalSkipReport

logger = logging.getLogger("harborrag.adapters.connectors.local")

_READ_CHUNK_SIZE = 1024 * 1024


class LocalFileSystem:
    """Traversal, selection and secure reads for one local source scope.

    Holds a descriptor on the root directory where the platform supports it; ``close``
    releases it.
    """

    def __init__(self, config: LocalFileConfig) -> None:
        """Resolve the source and root paths and open the root descriptor.

        Args:
            config: Validated local config.
        """
        self.config = config
        self.skips = LocalSkipReport()
        # Config accepts strings at the package boundary; traversal uses one
        # concrete Path representation after validation.
        self.source_path = Path(config.source_path)
        self._source_is_file = self.source_path.is_file()
        self.root_path = self.source_path.parent if self._source_is_file else self.source_path
        self._closed = False
        if SUPPORTS_DIR_FD:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            self._root_fd = os.open(self.root_path, flags)
        else:
            # No dir_fd support (Windows): reads fall back to plain,
            # per-component symlink-checked opens in `secure_read.py`.
            self._root_fd = -1
        self._selector = LocalFileSelector(
            config,
            self.root_path,
            self.skips,
            within_source_scope=self.within_source_scope,
            has_symlink_component=self.has_symlink_component,
        )

    def close(self) -> None:
        """Close the root descriptor. Later reads raise ``RuntimeError``."""

        if SUPPORTS_DIR_FD and self._root_fd >= 0:
            os.close(self._root_fd)
            self._root_fd = -1
        self._closed = True

    def files_from_query(self, query: ConnectorQuery) -> Iterator[tuple[Path, bool]]:
        """Yield ``(resolved path, is_symlink)`` for the explicit paths or the traversal.

        ``is_symlink`` is captured before the path is resolved.

        Raises:
            DocumentProcessingError: If an explicit path is outside the source scope or uses
                a symlink while ``follow_symlinks`` is off.
        """
        file_paths = file_paths_from_query(query)
        if file_paths:
            for path in file_paths:
                is_symlink = Path(path).is_symlink()
                resolved = self.resolve_candidate(path)
                if self.should_process_file(resolved, query):
                    yield resolved, is_symlink
            return

        start_path = self.start_path(query)
        logger.info("Discovering local files under %s", start_path)
        for path, is_symlink in self.iter_files(start_path, query=query):
            if self.should_process_file(path, query):
                yield path, is_symlink

    def iter_files(
        self,
        start_path: Path,
        *,
        query: ConnectorQuery,
        depth: int = 0,
        seen_dirs: set[Path] | None = None,
    ) -> Iterator[tuple[Path, bool]]:
        """Yield ``(resolved path, is_symlink)`` for files under ``start_path``.

        Args:
            start_path: File or directory to start from.
            query: Discovery query; ``recursive`` controls descent.
            depth: Current depth below the start path.
            seen_dirs: Resolved directories already visited.
        """
        if not self._traversable(start_path):
            return
        if start_path.is_file():
            yield start_path, start_path.is_symlink()
            return

        seen_dirs = seen_dirs or set()
        directory_key = resolve_path(start_path)
        if directory_key in seen_dirs:
            logger.debug("Skipping already visited local directory %s", start_path)
            return
        seen_dirs.add(directory_key)

        entries = self._directory_entries(start_path)
        if entries is None:
            return

        for entry in entries:
            yield from self._walk_entry(entry, query, depth=depth, seen_dirs=seen_dirs)

    def _traversable(self, path: Path) -> bool:
        """Return whether traversal may enter the path.

        Skips symlinks silently unless followed, and records out-of-scope paths as skips.

        Raises:
            DocumentProcessingError: If the path is neither a file nor a directory.
        """
        if path.is_symlink() and not self.config.follow_symlinks:
            return False
        if not self.within_source_scope(path):
            self.skips.out_of_scope(path)
            return False
        if not path.is_file() and not path.is_dir():
            raise DocumentProcessingError(f"Local path is not a file or directory: {path}")
        return True

    def _directory_entries(self, path: Path) -> list[Path] | None:
        """List a directory sorted by lowercase name, or return ``None`` after recording a skip.

        Raises:
            FetchError: If the listing fails and ``fail_on_error`` is set.
        """
        try:
            return sorted(path.iterdir(), key=lambda item: item.name.lower())
        except OSError as exc:
            if self.config.fail_on_error:
                raise FetchError(f"Could not list local directory {path}: {exc}") from exc
            self.skips.unreadable(path, error=exc)
            return None

    def _walk_entry(
        self,
        entry: Path,
        query: ConnectorQuery,
        *,
        depth: int,
        seen_dirs: set[Path],
    ) -> Iterator[tuple[Path, bool]]:
        """Yield ``(resolved path, is_symlink)`` for one entry, descending into directories.

        Hidden entries and unfollowed symlinks are ignored silently; a file resolving
        outside the source scope is recorded as a skip.
        """
        is_symlink = entry.is_symlink()
        if is_symlink and not self.config.follow_symlinks:
            return
        if not self.config.include_hidden and is_hidden_path(entry, self.root_path):
            return
        if entry.is_dir():
            depth_allowed = self.config.max_depth is None or depth < self.config.max_depth
            if (
                entry.name not in self.config.excluded_dir_names
                and query.recursive
                and depth_allowed
            ):
                yield from self.iter_files(
                    entry,
                    query=query,
                    depth=depth + 1,
                    seen_dirs=seen_dirs,
                )
            return
        if not entry.is_file():
            return
        resolved = resolve_path(entry)
        if not self.within_source_scope(resolved):
            self.skips.out_of_scope(entry)
            return
        yield resolved, is_symlink

    def should_process_file(self, path: Path, query: ConnectorQuery) -> bool:
        """Return whether a path passes the symlink rule and the selection policy."""
        if not self.config.follow_symlinks and self.has_symlink_component(path):
            return False
        return self._selector.should_process_file(path, query)

    def enforce_size_limit(self, path: Path, size: int) -> None:
        """Raise ``DocumentProcessingError`` if ``size`` exceeds ``max_file_size_bytes``."""
        if self.config.max_file_size_bytes is None:
            return
        if size > self.config.max_file_size_bytes:
            raise DocumentProcessingError(
                f"Local file {path} size {size} exceeds "
                f"max_file_size_bytes {self.config.max_file_size_bytes}"
            )

    def read_capped_bytes(self, path: Path) -> bytes:
        """Return a file's bytes, enforcing ``max_file_size_bytes`` during the read."""
        return self.read_snapshot(path).content

    def read_snapshot(self, path: Path) -> LocalFileSnapshot:
        """Read a file's bytes, stat and SHA-256 from one descriptor opened beneath the root.

        With ``dir_fd`` support every component is opened with ``O_NOFOLLOW``; otherwise
        symlinks are checked per component before a plain open.

        Raises:
            DocumentProcessingError: If the path is outside scope, not a regular file, or
                oversized.
            FetchError: If the file cannot be opened.
            RuntimeError: If the filesystem is closed.
        """

        return read_snapshot_beneath(
            path,
            scope=SecureReadScope(
                root_path=self.root_path,
                source_path=self.source_path,
                source_is_file=self._source_is_file,
                root_fd=self._root_fd,
                closed=self._closed,
            ),
            enforce_size_limit=self.enforce_size_limit,
            read_descriptor=self._read_descriptor,
        )

    def _read_descriptor(
        self,
        path: Path,
        file_fd: int,
        descriptor_stat: os.stat_result,
    ) -> LocalFileSnapshot:
        """Read and hash an open descriptor. Raises ``DocumentProcessingError`` if oversized."""

        limit = self.config.max_file_size_bytes
        buffer = bytearray()
        digest = sha256()
        with os.fdopen(os.dup(file_fd), "rb") as handle:
            while True:
                chunk = handle.read(_READ_CHUNK_SIZE)
                if not chunk:
                    break
                buffer.extend(chunk)
                digest.update(chunk)
                if limit is not None and len(buffer) > limit:
                    raise DocumentProcessingError(
                        f"Local file {path} exceeds max_file_size_bytes {limit}"
                    )
        return LocalFileSnapshot(
            content=bytes(buffer),
            stat=descriptor_stat,
            checksum=digest.hexdigest(),
        )

    def source_record(self, path: Path, *, is_symlink: bool) -> SourceRecord:
        """Build the source record for a file, with the configured checksum."""
        return build_source_record(
            path,
            root_path=self.root_path,
            checksum=self.checksum(path),
            is_symlink=is_symlink,
        )

    def discovered_record(self, path: Path, *, is_symlink: bool) -> SourceRecord | None:
        """Build a file's record, or record a skip and return ``None`` if it became unreadable.

        Args:
            path: Resolved file path.
            is_symlink: Whether the path was a symlink before resolution.

        Raises:
            FetchError: If the file cannot be read and ``fail_on_error`` is set.
            DocumentProcessingError: If the file became oversized and ``fail_on_error`` is set.
        """
        try:
            return self.source_record(path, is_symlink=is_symlink)
        except OSError as exc:
            if self.config.fail_on_error:
                raise FetchError(f"Could not stat local file {path}: {exc}") from exc
            self.skips.unreadable(path, error=exc)
        except (FetchError, DocumentProcessingError) as exc:
            if self.config.fail_on_error:
                raise
            self.skips.unreadable(path, error=exc)
        return None

    def record_for_path(self, path: str | Path) -> SourceRecord:
        """Return the source record for a caller-provided path.

        Applies the discovery selection policy before opening the file.

        Raises:
            DocumentProcessingError: If the path is outside scope or excluded by policy.
        """
        is_symlink = Path(path).is_symlink()
        resolved = self.resolve_candidate(path)
        if not self.should_process_file(resolved, ConnectorQuery()):
            raise DocumentProcessingError(f"Local path is excluded by connector policy: {path}")
        return self.source_record(resolved, is_symlink=is_symlink)

    def checksum(self, path: Path) -> str | None:
        """Return the change-detection signature for ``checksum_mode``, or ``None`` for ``none``.

        The ``sha256`` mode reads through ``read_snapshot``.
        """
        if self.config.checksum_mode == "none":
            return None
        if self.config.checksum_mode == "sha256":
            return self.read_snapshot(path).checksum
        return stat_signature(path)

    def start_path(self, query: ConnectorQuery) -> Path:
        """Return the traversal root: the query path if set, else the source path."""
        if query.path:
            return self.resolve_candidate(query.path)
        return self.source_path

    def resolve_candidate(self, value: str | Path) -> Path:
        """Return the resolved path for a user or record path.

        Raises:
            DocumentProcessingError: If it uses a symlink while ``follow_symlinks`` is off,
                or resolves outside the source scope.
        """
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = self.root_path / candidate
        if not self.config.follow_symlinks and self.has_symlink_component(candidate):
            raise DocumentProcessingError(f"Local symlinks are disabled for source scope: {value}")
        resolved = resolve_path(candidate)
        if not self.within_source_scope(resolved):
            raise DocumentProcessingError(f"Local path is outside configured source scope: {value}")
        return resolved

    def within_source_scope(self, path: Path) -> bool:
        """Return whether a path resolves inside the source scope."""
        resolved = resolve_path(path)
        if self._source_is_file:
            return resolved == self.source_path
        return resolved.is_relative_to(self.source_path)

    def has_symlink_component(self, path: Path) -> bool:
        """Return whether the path or any parent up to the root is a symlink."""
        current = path
        while True:
            if current.is_symlink():
                return True
            if current == self.root_path or current == current.parent:
                return False
            current = current.parent
