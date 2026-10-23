"""Config and query filters for GitHub repository discovery.

Handles:
- Update window, query pattern, extension, path and glob filters
- Size limit and ``process_file_callback``
- Explicit file paths from query filters
"""

from __future__ import annotations

import logging
from typing import Any

from harborrag_adapters.connectors.schemas import ConnectorQuery

from .config import GitHubRepositoryConfig
from .mappers import commit_timestamp
from .repository_paths import (
    file_extension,
    guess_mime_type,
    normalize_remote_path,
    normalize_repo_path,
    path_in_scope,
    path_matches_patterns,
    path_matches_query,
)

logger = logging.getLogger("harborrag.adapters.connectors.github")


def should_process_file(
    config: GitHubRepositoryConfig,
    item: dict[str, Any],
    query: ConnectorQuery,
    *,
    commit: dict[str, Any],
) -> bool:
    """Return whether a tree item passes every config and query filter.

    Query filters override the config values of the same name.

    Args:
        config: Repository config.
        item: Tree or content item with ``path`` and ``size``.
        query: Discovery query.
        commit: Commit the item belongs to.
    """
    path = normalize_remote_path(str(item.get("path") or ""))
    size = int(item.get("size") or 0)
    extension = file_extension(path)

    if not _within_update_window(query, commit):
        return False
    if not path_matches_query(path, query.pattern):
        return False
    if not _extension_allowed(config, query, extension):
        return False
    if not _path_allowed(config, query, path):
        return False
    if not _glob_allowed(config, query, path):
        return False

    if config.max_file_size_bytes is not None and size > config.max_file_size_bytes:
        logger.debug("Skipping oversized GitHub file %s", path)
        return False
    return _callback_allows_file(config, path, size)


def _within_update_window(query: ConnectorQuery, commit: dict[str, Any]) -> bool:
    """Return whether the commit is newer than ``updated_after``. Unknown dates pass."""
    if query.updated_after is None:
        return True
    updated_at = commit_timestamp(commit)
    return updated_at is None or updated_at > query.updated_after


def _extension_allowed(
    config: GitHubRepositoryConfig,
    query: ConnectorQuery,
    extension: str,
) -> bool:
    """Apply the allowed and excluded extension sets. An empty allow set admits all."""
    allowed = _extension_filter(config, query, "allowed_extensions")
    excluded = _extension_filter(config, query, "excluded_extensions")
    return (not allowed or extension in allowed) and extension not in excluded


def _path_allowed(
    config: GitHubRepositoryConfig,
    query: ConnectorQuery,
    path: str,
) -> bool:
    """Apply include and exclude path prefixes. No include paths admits all."""
    included = _path_filter(config, query, "include_paths")
    excluded = _path_filter(config, query, "exclude_paths")
    is_included = not included or any(
        path_in_scope(path, value, recursive=True) for value in included
    )
    is_excluded = any(path_in_scope(path, value, recursive=True) for value in excluded)
    return is_included and not is_excluded


def _glob_allowed(
    config: GitHubRepositoryConfig,
    query: ConnectorQuery,
    path: str,
) -> bool:
    """Apply include and exclude globs. No include globs admits all."""
    included = _path_filter(config, query, "include_globs")
    excluded = _path_filter(config, query, "exclude_globs")
    return (not included or path_matches_patterns(path, included)) and not path_matches_patterns(
        path, excluded
    )


def file_paths_from_query(query: ConnectorQuery) -> list[str]:
    """Return the explicit file paths from the ``file_paths``, ``paths`` or ``files`` filter.

    Raises:
        ValueError: If a path contains a ``..`` segment.
    """
    values = (
        query.filters.get("file_paths") or query.filters.get("paths") or query.filters.get("files")
    )
    if values is None:
        return []
    if isinstance(values, str):
        return [normalize_repo_path(values)]
    return [normalize_repo_path(str(value)) for value in values]


def _callback_allows_file(
    config: GitHubRepositoryConfig,
    path: str,
    size: int,
) -> bool:
    """Ask ``process_file_callback``. A failing callback skips the file unless ``fail_on_error``."""
    if not config.process_file_callback:
        return True
    try:
        should_process, reason = config.process_file_callback(
            path,
            size,
            guess_mime_type(path),
        )
    except Exception:
        if config.fail_on_error:
            raise
        logger.exception("GitHub file callback failed for %s", path)
        return False
    if not should_process:
        logger.debug("Skipping GitHub file %s: %s", path, reason)
    return should_process


def _extension_filter(
    config: GitHubRepositoryConfig,
    query: ConnectorQuery,
    key: str,
) -> set[str]:
    """Return the extension set for ``key``, preferring the query over the config.

    The ``extensions`` query key is an alias for ``allowed_extensions``.
    """
    values = query.filters.get(key)
    if values is None and key == "allowed_extensions":
        values = query.filters.get("extensions")
    if values is None:
        return set(getattr(config, key))
    if isinstance(values, str):
        values = [values]
    return {
        (
            str(value).lower().strip()
            if str(value).startswith(".")
            else f".{str(value).lower().strip()}"
        )
        for value in values
    }


def _path_filter(
    config: GitHubRepositoryConfig,
    query: ConnectorQuery,
    key: str,
) -> list[str]:
    """Return the path or glob list for ``key``, preferring the query over the config."""
    values = query.filters.get(key)
    if values is None:
        return list(getattr(config, key))
    if isinstance(values, str):
        return [normalize_repo_path(values)]
    return [normalize_repo_path(str(value)) for value in values]
