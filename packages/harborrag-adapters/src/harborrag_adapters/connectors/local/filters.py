"""Query-filter normalization for local filesystem discovery."""

from __future__ import annotations

from pathlib import Path

from harborrag_adapters.connectors.schemas import ConnectorQuery

from .config import LocalFileConfig


def extension_filter(
    config: LocalFileConfig,
    query: ConnectorQuery,
    key: str,
) -> set[str]:
    """Return the extension set for ``key`` from the query, else from the config.

    The ``extensions`` query key is an alias for ``allowed_extensions``.
    """
    # TODO: narrow the config filter with the query filter instead of replacing it.
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


def path_filter(
    config: LocalFileConfig,
    query: ConnectorQuery,
    key: str,
) -> list[str]:
    """Return the path or glob list for ``key`` from the query, else from the config."""
    # TODO: narrow the config filter with the query filter instead of replacing it.
    values = query.filters.get(key)
    if values is None:
        return list(getattr(config, key))
    if isinstance(values, str):
        values = [values]
    return [str(value).replace("\\", "/").strip("/") for value in values]


def file_paths_from_query(query: ConnectorQuery) -> list[str | Path]:
    """Return the explicit file paths from the ``file_paths``, ``paths`` or ``files`` filter."""
    values = (
        query.filters.get("file_paths") or query.filters.get("paths") or query.filters.get("files")
    )
    if values is None:
        return []
    if isinstance(values, (str, Path)):
        return [values]
    return [str(value) for value in values]
