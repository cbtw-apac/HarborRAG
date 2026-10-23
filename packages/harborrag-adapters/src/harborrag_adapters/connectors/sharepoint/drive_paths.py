"""Path, endpoint and drive-item helpers for SharePoint.

Handles:
- Graph field selections
- Site URL parsing and endpoint builders
- Drive path normalization and root scoping
- Drive item type, name and path accessors
"""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlparse

DRIVE_ITEM_SELECT = ",".join(
    (
        "id",
        "name",
        "webUrl",
        "size",
        "file",
        "folder",
        "package",
        "hidden",
        "parentReference",
        "createdDateTime",
        "lastModifiedDateTime",
        "createdBy",
        "lastModifiedBy",
        "eTag",
        "cTag",
        "@microsoft.graph.downloadUrl",
    )
)

DRIVE_SELECT = "id,name,driveType,webUrl,createdDateTime,lastModifiedDateTime"


def parse_sharepoint_site_url(site_url: str) -> tuple[str, str]:
    """Return ``(hostname, site path)`` from an absolute SharePoint site URL.

    Raises:
        ValueError: If the URL is not absolute http(s).
    """
    parsed = urlparse(site_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("site_url must be an absolute SharePoint URL")

    path = unquote(parsed.path).strip("/")
    return parsed.hostname, path


def site_path_endpoint(hostname: str, site_path: str | None) -> str:
    """Return the Graph endpoint that resolves a site by hostname and path."""
    path = (site_path or "").strip("/")
    if not path:
        return f"sites/{hostname}:/"
    return f"sites/{hostname}:/{path}"


def children_endpoint(
    drive_id: str,
    *,
    item_id: str | None = None,
    path: str | None = None,
) -> str:
    """Return the Graph children endpoint for a folder item id or path.

    Raises:
        ValueError: If ``path`` contains a ``..`` segment.
    """
    if item_id:
        # TODO: URL-encode item ids in Graph endpoints.
        return f"drives/{drive_id}/items/{item_id}/children"
    normalized_path = normalize_drive_path(path)
    if normalized_path:
        encoded_path = quote(normalized_path, safe="/")
        return f"drives/{drive_id}/root:/{encoded_path}:/children"
    return f"drives/{drive_id}/root/children"


def normalize_drive_path(path: str | None) -> str:
    """Normalize a drive path to a POSIX-style path without outer slashes.

    Raises:
        ValueError: If the path contains a ``..`` segment.
    """
    if not path:
        return ""
    normalized = path.strip("/")
    if ".." in normalized.split("/"):
        raise ValueError(f"SharePoint drive path must not contain '..' segments: {path!r}")
    return str(PurePosixPath(normalized)) if normalized else ""


def is_drive_file(item: dict[str, Any]) -> bool:
    """Return whether a drive item is a file."""
    return isinstance(item.get("file"), dict)


def is_drive_folder(item: dict[str, Any]) -> bool:
    """Return whether a drive item is a folder or package."""
    return isinstance(item.get("folder"), dict) or isinstance(item.get("package"), dict)


def item_name(item: dict[str, Any]) -> str:
    """Return the item's name, or an empty string."""
    return str(item.get("name") or "")


def item_extension(item: dict[str, Any]) -> str:
    """Return the lowercased extension of the item's name, or an empty string."""
    name = item_name(item)
    if "." not in name:
        return ""
    return PurePosixPath(name).suffix.lower()


def item_mime_type(item: dict[str, Any]) -> str:
    """Return the item's MIME type, with a folder type for folders and a binary default."""
    file_info = item.get("file")
    if isinstance(file_info, dict):
        return str(file_info.get("mimeType") or "application/octet-stream")
    if is_drive_folder(item):
        return "application/vnd.microsoft.graph.folder"
    return "application/octet-stream"


def item_hidden(item: dict[str, Any]) -> bool:
    """Return whether Graph marks the item as hidden."""
    value = item.get("hidden")
    return isinstance(value, dict) or value is True


def item_path(item: dict[str, Any]) -> str:
    """Return the item's path under the drive root, built from ``parentReference``."""
    name = item_name(item)
    parent_path = str(item.get("parentReference", {}).get("path") or "")
    if "root:" not in parent_path:
        return name

    folder_path = parent_path.split("root:", 1)[1].strip("/")
    if not folder_path:
        return name
    return f"{folder_path}/{name}".strip("/")


def scoped_drive_root(configured: str | None, requested: str | None) -> str:
    """Return the decoded traversal root; a query path may narrow the config root, never widen it.

    Both paths are percent-decoded once; the decoded form is returned.

    Args:
        configured: ``root_path`` from the config.
        requested: Path from the query.

    Raises:
        ValueError: If ``requested`` is outside ``configured`` or contains ``..``.
    """
    root = normalize_drive_path(unquote(configured or ""))
    if not requested:
        return root
    candidate = normalize_drive_path(unquote(requested))
    if root and not _path_under(candidate, root):
        raise ValueError(f"query path {requested!r} is outside configured root_path {configured!r}")
    return candidate


def item_within_root(item: dict[str, Any], root_path: str | None) -> bool:
    """Return whether an item is under ``root_path``. An item without a parent path is outside."""
    root = normalize_drive_path(unquote(root_path or ""))
    if not root:
        return True
    parent_path = str((item.get("parentReference") or {}).get("path") or "")
    if "root:" not in parent_path:
        return False
    return _path_under(unquote(item_path(item)), root)


def _path_under(path: str, root: str) -> bool:
    """Return whether a decoded path equals or is under ``root``, ignoring case."""
    candidate = path.casefold()
    folded_root = root.casefold()
    return candidate == folded_root or candidate.startswith(f"{folded_root}/")


def matches_pattern(item: dict[str, Any], pattern: str | None) -> bool:
    """Return whether the item name matches a query glob, or contains the pattern."""
    if not pattern:
        return True
    name = item_name(item).lower()
    normalized = pattern.lower()
    if any(char in normalized for char in "*?[]"):
        return fnmatch(name, normalized)
    return normalized in name
