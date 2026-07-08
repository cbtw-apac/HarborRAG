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
    """Parse a SharePoint site URL into Graph hostname and site path."""
    parsed = urlparse(site_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("site_url must be an absolute SharePoint URL")

    path = unquote(parsed.path).strip("/")
    return parsed.hostname, path


def site_path_endpoint(hostname: str, site_path: str | None) -> str:
    """Build the Graph endpoint that resolves a SharePoint site by path."""
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
    """Build the Graph children endpoint for a folder item or path."""
    if item_id:
        return f"drives/{drive_id}/items/{item_id}/children"
    normalized_path = normalize_drive_path(path)
    if normalized_path:
        encoded_path = quote(normalized_path, safe="/")
        return f"drives/{drive_id}/root:/{encoded_path}:/children"
    return f"drives/{drive_id}/root/children"


def normalize_drive_path(path: str | None) -> str:
    """Normalize SharePoint drive paths to Graph's POSIX-like path style."""
    if not path:
        return ""
    normalized = path.strip("/")
    return str(PurePosixPath(normalized)) if normalized else ""


def is_drive_file(item: dict[str, Any]) -> bool:
    """Return whether a Graph drive item is a downloadable file."""
    return isinstance(item.get("file"), dict)


def is_drive_folder(item: dict[str, Any]) -> bool:
    """Return whether a Graph drive item can contain children."""
    return isinstance(item.get("folder"), dict) or isinstance(item.get("package"), dict)


def item_name(item: dict[str, Any]) -> str:
    """Return the display name of a Graph drive item."""
    return str(item.get("name") or "")


def item_extension(item: dict[str, Any]) -> str:
    """Return the lowercased file extension for a drive item."""
    name = item_name(item)
    if "." not in name:
        return ""
    return PurePosixPath(name).suffix.lower()


def item_mime_type(item: dict[str, Any]) -> str:
    """Return a MIME type for a Graph drive item."""
    file_info = item.get("file")
    if isinstance(file_info, dict):
        return str(file_info.get("mimeType") or "application/octet-stream")
    if is_drive_folder(item):
        return "application/vnd.microsoft.graph.folder"
    return "application/octet-stream"


def item_hidden(item: dict[str, Any]) -> bool:
    """Return whether Graph marks a drive item as hidden."""
    value = item.get("hidden")
    return isinstance(value, dict) or value is True


def item_path(item: dict[str, Any]) -> str:
    """Reconstruct a path-like locator from Graph parentReference metadata."""
    name = item_name(item)
    parent_path = str(item.get("parentReference", {}).get("path") or "")
    if "root:" not in parent_path:
        return name

    folder_path = parent_path.split("root:", 1)[1].strip("/")
    if not folder_path:
        return name
    return f"{folder_path}/{name}".strip("/")


def matches_pattern(item: dict[str, Any], pattern: str | None) -> bool:
    """Return whether a drive item name matches a query pattern."""
    if not pattern:
        return True
    name = item_name(item).lower()
    normalized = pattern.lower()
    if any(char in normalized for char in "*?[]"):
        return fnmatch(name, normalized)
    return normalized in name
