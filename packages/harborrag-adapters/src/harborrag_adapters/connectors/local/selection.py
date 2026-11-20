"""Local-file candidate selection kept separate from traversal and secure reads.

Only an exclusion a caller would want named becomes a skip: an unreadable file,
a size limit, an extension policy or a callback rejection. Date, pattern, path
and glob filters narrow the scope silently, and run before any reporting check.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from harborrag_adapters.connectors.exceptions import DocumentProcessingError, FetchError
from harborrag_core.ingestion.connector_contracts import ConnectorQuery

from .config import LocalFileConfig
from .filesystem_paths import (
    file_extension,
    guess_mime_type,
    is_hidden_path,
    matches_globs,
    matches_pattern,
    path_in_scope,
    relative_path,
    stat_datetime,
)
from .filters import extension_filter, path_filter
from .skips import LocalSkipReport


class LocalFileSelector:
    """Apply query, size, link, and callback policy to local candidates.

    Args:
        config: The local connector's settings.
        root_path: Root that relative paths and filters are resolved against.
        skips: Report that receives every reportable skip.
        within_source_scope: Whether a path stays inside the configured source.
        has_symlink_component: Whether any component of a path is a symlink.
    """

    def __init__(
        self,
        config: LocalFileConfig,
        root_path: Path,
        skips: LocalSkipReport,
        *,
        within_source_scope: Callable[[Path], bool],
        has_symlink_component: Callable[[Path], bool],
    ) -> None:
        self.config = config
        self.root_path = root_path
        self.skips = skips
        self._within_source_scope = within_source_scope
        self._has_symlink_component = has_symlink_component

    def should_process_file(self, path: Path, query: ConnectorQuery) -> bool:
        """Apply all configured selection policy to one candidate path."""
        if not self._is_candidate(path):
            return False
        file_stat = self._candidate_stat(path)
        if file_stat is None:
            return False
        if not self._matches_query(path, query, file_stat):
            return False
        if not self._within_size_limit(path, file_stat.st_size):
            return False
        return self._callback_allows(path, file_stat.st_size)

    def _is_candidate(self, path: Path) -> bool:
        if not path.is_file():
            return False
        if not self.config.follow_symlinks and self._has_symlink_component(path):
            return False
        if not self._within_source_scope(path):
            raise DocumentProcessingError(
                f"Local path is outside configured source scope {self.root_path}: {path}"
            )
        return self.config.include_hidden or not is_hidden_path(path, self.root_path)

    def _candidate_stat(self, path: Path) -> os.stat_result | None:
        try:
            return path.stat()
        except OSError as exc:
            if self.config.fail_on_error:
                raise FetchError(f"Could not stat local file {path}: {exc}") from exc
            self.skips.unreadable(path, error=exc)
            return None

    def _matches_query(
        self,
        path: Path,
        query: ConnectorQuery,
        file_stat: os.stat_result,
    ) -> bool:
        """Apply the query's filters, checking the extension last."""
        updated_at = stat_datetime(file_stat.st_mtime)
        if query.updated_after and updated_at <= query.updated_after:
            return False
        if not matches_pattern(path, self.root_path, query.pattern):
            return False
        return self._in_path_scope(path, query) and self._extension_allowed(path, query)

    def _in_path_scope(self, path: Path, query: ConnectorQuery) -> bool:
        """Apply include and exclude paths and globs, which drop files silently."""
        include_paths = path_filter(self.config, query, "include_paths")
        if include_paths and not any(
            path_in_scope(path, self.root_path, value) for value in include_paths
        ):
            return False
        exclude_paths = path_filter(self.config, query, "exclude_paths")
        if any(path_in_scope(path, self.root_path, value) for value in exclude_paths):
            return False
        include_globs = path_filter(self.config, query, "include_globs")
        if include_globs and not matches_globs(path, self.root_path, include_globs):
            return False
        exclude_globs = path_filter(self.config, query, "exclude_globs")
        return not matches_globs(path, self.root_path, exclude_globs)

    def _extension_allowed(self, path: Path, query: ConnectorQuery) -> bool:
        """Apply the extension allow and exclude lists, reporting each file they drop."""
        extension = file_extension(path)
        allowed = extension_filter(self.config, query, "allowed_extensions")
        if allowed and extension not in allowed:
            self.skips.unsupported_extension(path, extension=extension, reason="is not allowed")
            return False
        excluded = extension_filter(self.config, query, "excluded_extensions")
        if extension in excluded:
            self.skips.unsupported_extension(path, extension=extension, reason="is excluded")
            return False
        return True

    def _within_size_limit(self, path: Path, size: int) -> bool:
        limit = self.config.max_file_size_bytes
        if limit is not None and size > limit:
            self.skips.oversized(path, size=size, limit=limit)
            return False
        return True

    def _callback_allows(self, path: Path, size: int) -> bool:
        callback = self.config.process_file_callback
        if callback is None:
            return True
        try:
            should_process, reason = callback(
                relative_path(path, self.root_path),
                size,
                guess_mime_type(path),
            )
        except Exception as exc:
            if self.config.fail_on_error:
                raise
            self.skips.callback_failed(path, error=exc)
            return False
        if not should_process:
            self.skips.callback_rejected(path, reason=reason)
        return should_process
