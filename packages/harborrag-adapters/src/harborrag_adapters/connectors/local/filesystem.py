"""Safe local filesystem traversal, filtering, and scope enforcement."""

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
    file_extension,
    guess_mime_type,
    is_hidden_path,
    matches_globs,
    matches_pattern,
    path_in_scope,
    relative_path,
    resolve_path,
    stat_datetime,
    stat_signature,
)
from .filters import extension_filter, file_paths_from_query, path_filter
from .mappers import build_source_record
from .secure_read import LocalFileSnapshot, SecureReadScope, read_snapshot_beneath
from .skips import LocalSkipReport

logger = logging.getLogger("harborrag.adapters.connectors.local")

_READ_CHUNK_SIZE = 1024 * 1024


class LocalFileSystem:
    """Filesystem traversal, filtering, and scope enforcement for local files."""

    def __init__(self, config: LocalFileConfig) -> None:
        """Normalize the configured source into concrete traversal paths."""
        self.config = config
        self.skips = LocalSkipReport()
        # Config accepts strings at the package boundary; traversal uses one
        # concrete Path representation after validation.
        self.source_path = Path(config.source_path)
        self._source_is_file = self.source_path.is_file()
        self.root_path = self.source_path.parent if self._source_is_file else self.source_path
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        self._root_fd = os.open(self.root_path, flags)

    def close(self) -> None:
        """Release the trusted root descriptor used for race-free reads."""

        if self._root_fd >= 0:
            os.close(self._root_fd)
            self._root_fd = -1

    def files_from_query(self, query: ConnectorQuery) -> Iterator[tuple[Path, bool]]:
        """Yield resolved files (with pre-resolution symlink provenance)."""
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
        """Yield files with symlink provenance under the configured traversal policy."""
        if start_path.is_symlink() and not self.config.follow_symlinks:
            return
        if not self.within_source_scope(start_path):
            self.skips.out_of_scope(start_path)
            return
        if start_path.is_file():
            yield start_path, start_path.is_symlink()
            return
        if not start_path.is_dir():
            raise DocumentProcessingError(f"Local path is not a file or directory: {start_path}")

        seen_dirs = seen_dirs or set()
        directory_key = resolve_path(start_path)
        if directory_key in seen_dirs:
            logger.debug("Skipping already visited local directory %s", start_path)
            return
        seen_dirs.add(directory_key)

        try:
            entries = sorted(start_path.iterdir(), key=lambda item: item.name.lower())
        except OSError as exc:
            if self.config.fail_on_error:
                raise FetchError(f"Could not list local directory {start_path}: {exc}") from exc
            self.skips.unreadable(start_path, error=exc)
            return

        for entry in entries:
            is_symlink_entry = entry.is_symlink()
            if is_symlink_entry and not self.config.follow_symlinks:
                continue
            if not self.config.include_hidden and is_hidden_path(entry, self.root_path):
                continue

            if entry.is_dir():
                if entry.name in self.config.excluded_dir_names:
                    continue
                if not query.recursive:
                    continue
                if self.config.max_depth is not None and depth >= self.config.max_depth:
                    continue
                yield from self.iter_files(
                    entry,
                    query=query,
                    depth=depth + 1,
                    seen_dirs=seen_dirs,
                )
                continue

            if entry.is_file():
                resolved = resolve_path(entry)
                if not self.within_source_scope(resolved):
                    self.skips.out_of_scope(entry)
                    continue
                yield resolved, is_symlink_entry

    def should_process_file(self, path: Path, query: ConnectorQuery) -> bool:
        """Apply local connector filters to one candidate path."""
        if not path.is_file():
            return False
        if not self.config.follow_symlinks and self.has_symlink_component(path):
            return False
        if not self.within_source_scope(path):
            raise DocumentProcessingError(f"Local path is outside configured source scope: {path}")
        if not self.config.include_hidden and is_hidden_path(path, self.root_path):
            return False

        try:
            stat = path.stat()
        except OSError as exc:
            if self.config.fail_on_error:
                raise FetchError(f"Could not stat local file {path}: {exc}") from exc
            self.skips.unreadable(path, error=exc)
            return False

        updated_at = stat_datetime(stat.st_mtime)
        if query.updated_after and updated_at <= query.updated_after:
            return False
        if not matches_pattern(path, self.root_path, query.pattern):
            return False

        extension = file_extension(path)
        allowed_extensions = extension_filter(self.config, query, "allowed_extensions")
        if allowed_extensions and extension not in allowed_extensions:
            return False
        excluded_extensions = extension_filter(self.config, query, "excluded_extensions")
        if extension in excluded_extensions:
            return False

        include_paths = path_filter(self.config, query, "include_paths")
        if include_paths:
            if not any(path_in_scope(path, self.root_path, value) for value in include_paths):
                return False
        exclude_paths = path_filter(self.config, query, "exclude_paths")
        if any(path_in_scope(path, self.root_path, value) for value in exclude_paths):
            return False

        include_globs = path_filter(self.config, query, "include_globs")
        if include_globs and not matches_globs(path, self.root_path, include_globs):
            return False
        exclude_globs = path_filter(self.config, query, "exclude_globs")
        if matches_globs(path, self.root_path, exclude_globs):
            return False

        size_limit = self.config.max_file_size_bytes
        if size_limit is not None and stat.st_size > size_limit:
            self.skips.oversized(path, size=stat.st_size, limit=size_limit)
            return False

        if self.config.process_file_callback:
            try:
                should_process, reason = self.config.process_file_callback(
                    relative_path(path, self.root_path),
                    stat.st_size,
                    guess_mime_type(path),
                )
            except Exception as exc:
                if self.config.fail_on_error:
                    raise
                logger.exception("Local file callback failed for %s", path)
                self.skips.callback_rejected(path, reason=f"raised {type(exc).__name__}: {exc}")
                return False
            if not should_process:
                self.skips.callback_rejected(path, reason=reason)
                return False
        return True

    def enforce_size_limit(self, path: Path, size: int) -> None:
        """Prevent direct loads from materializing oversized files."""
        if self.config.max_file_size_bytes is None:
            return
        if size > self.config.max_file_size_bytes:
            raise DocumentProcessingError(
                f"Local file {path} size {size} exceeds "
                f"max_file_size_bytes {self.config.max_file_size_bytes}"
            )

    def read_capped_bytes(self, path: Path) -> bytes:
        """Read a file while re-enforcing max_file_size_bytes during the read.

        A stat-then-read size check leaves a window where the file can grow
        between the two calls, so ``load()`` would materialize an unbounded
        file despite the earlier check. Re-checking the size incrementally
        while reading closes that race.
        """
        return self.read_snapshot(path).content

    def read_snapshot(self, path: Path) -> LocalFileSnapshot:
        """Open beneath the bound root and read/hash/fstat one descriptor.

        Every component is opened with ``O_NOFOLLOW``. This binds validation,
        bytes, metadata, and checksum to one inode even if a writer renames a
        pathname while the connector is loading it.
        """

        return read_snapshot_beneath(
            path,
            scope=SecureReadScope(
                root_path=self.root_path,
                source_path=self.source_path,
                source_is_file=self._source_is_file,
                root_fd=self._root_fd,
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
        """Read and hash an authorized descriptor with a hard byte cap."""

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
        """Build a lightweight source record for a discovered file."""
        return build_source_record(
            path,
            root_path=self.root_path,
            checksum=self.checksum(path),
            is_symlink=is_symlink,
        )

    def record_for_path(self, path: str | Path) -> SourceRecord:
        """Resolve a caller-provided path and return its source record."""
        is_symlink = Path(path).is_symlink()
        resolved = self.resolve_candidate(path)
        return self.source_record(resolved, is_symlink=is_symlink)

    def checksum(self, path: Path) -> str | None:
        """Return the configured change-detection signature for a file.

        The sha256 mode routes through ``read_snapshot`` rather than hashing
        the path directly, so it gets the same O_NOFOLLOW/dir_fd-bound
        descriptor and size cap as every other read -- hashing a plain
        ``path.open()`` would reopen the file outside that protection and
        reintroduce the stat-then-read TOCTOU window the rest of this module
        closes.
        """
        if self.config.checksum_mode == "none":
            return None
        if self.config.checksum_mode == "sha256":
            return self.read_snapshot(path).checksum
        return stat_signature(path)

    def start_path(self, query: ConnectorQuery) -> Path:
        """Resolve the traversal root selected by a query."""
        if query.path:
            return self.resolve_candidate(query.path)
        return self.source_path

    def resolve_candidate(self, value: str | Path) -> Path:
        """Resolve a user or record path and enforce the configured source scope."""
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
        """Return whether a resolved path is inside the configured source scope."""
        resolved = resolve_path(path)
        if self._source_is_file:
            return resolved == self.source_path
        return resolved.is_relative_to(self.source_path)

    def has_symlink_component(self, path: Path) -> bool:
        """Detect symlink use anywhere between the candidate and source root."""
        current = path
        while True:
            if current.is_symlink():
                return True
            if current == self.root_path or current == current.parent:
                return False
            current = current.parent
