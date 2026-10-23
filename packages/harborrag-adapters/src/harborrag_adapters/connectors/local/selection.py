"""Selection policy for local file candidates."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path

from harborrag_adapters.connectors.exceptions import DocumentProcessingError, FetchError
from harborrag_adapters.connectors.schemas import ConnectorQuery

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

logger = logging.getLogger("harborrag.adapters.connectors.local")


class LocalFileSelector:
    """Applies the query, size, symlink, hidden-file and callback policy to candidates."""

    def __init__(
        self,
        config: LocalFileConfig,
        root_path: Path,
        skips: LocalSkipReport,
        *,
        within_source_scope: Callable[[Path], bool],
        has_symlink_component: Callable[[Path], bool],
    ) -> None:
        """Bind config, root and skip report with the filesystem's scope and symlink checks.

        Args:
            config: Validated local config.
            root_path: Trusted root directory.
            skips: Report that receives skipped candidates.
            within_source_scope: Returns whether a path resolves inside the source scope.
            has_symlink_component: Returns whether a path or its parents include a symlink.
        """
        self.config = config
        self.root_path = root_path
        self.skips = skips
        self._within_source_scope = within_source_scope
        self._has_symlink_component = has_symlink_component

    def should_process_file(self, path: Path, query: ConnectorQuery) -> bool:
        """Return whether a candidate passes every selection rule, recording skips.

        Args:
            path: Resolved candidate path.
            query: Discovery query.

        Raises:
            DocumentProcessingError: If the path is outside the source scope.
            FetchError: If the file cannot be stat'ed and ``fail_on_error`` is set.
        """
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
        """Return whether the path is a regular file visible to this scan.

        Raises:
            DocumentProcessingError: If the path is outside the source scope.
        """
        if not path.is_file():
            return False
        if not self.config.follow_symlinks and self._has_symlink_component(path):
            return False
        if not self._within_source_scope(path):
            raise DocumentProcessingError(f"Local path is outside configured source scope: {path}")
        return self.config.include_hidden or not is_hidden_path(path, self.root_path)

    def _candidate_stat(self, path: Path) -> os.stat_result | None:
        """Return the file's ``stat`` result, or ``None`` after recording an unreadable skip.

        Raises:
            FetchError: If ``stat`` fails and ``fail_on_error`` is set.
        """
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
        """Apply the modified-time, pattern, extension, path and glob filters.

        Excluded paths and globs win over included ones.
        """
        updated_at = stat_datetime(file_stat.st_mtime)
        if query.updated_after and updated_at <= query.updated_after:
            return False
        if not matches_pattern(path, self.root_path, query.pattern):
            return False
        extension = file_extension(path)
        allowed = extension_filter(self.config, query, "allowed_extensions")
        excluded = extension_filter(self.config, query, "excluded_extensions")
        if (allowed and extension not in allowed) or extension in excluded:
            return False
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

    def _within_size_limit(self, path: Path, size: int) -> bool:
        """Return whether the file fits ``max_file_size_bytes``, recording a skip if not."""
        limit = self.config.max_file_size_bytes
        if limit is not None and size > limit:
            self.skips.oversized(path, size=size, limit=limit)
            return False
        return True

    def _callback_allows(self, path: Path, size: int) -> bool:
        """Ask ``process_file_callback``, recording a skip when it declines or raises.

        A raised error is re-raised if ``fail_on_error`` is set.
        """
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
            logger.exception("Local file callback failed for %s", path)
            self.skips.callback_rejected(path, reason=f"raised {type(exc).__name__}: {exc}")
            return False
        if not should_process:
            self.skips.callback_rejected(path, reason=reason)
        return should_process
