"""Startup validation helpers for SharePoint connector configuration."""

from __future__ import annotations

from collections.abc import Iterable
from functools import cache
from urllib.parse import urlparse

from .drive_paths import normalize_drive_path


def validate_site_url(site_url: str) -> None:
    """Require an absolute HTTPS SharePoint site URL."""
    parsed = urlparse(site_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("site_url must be an absolute https:// SharePoint URL")


def validate_positive_limit(name: str, value: int | None) -> None:
    """Reject a zero size limit; ``None`` means no limit."""
    if value is not None and value <= 0:
        raise ValueError(f"{name} must be greater than 0")


def validate_drive_path(name: str, value: str | None) -> str | None:
    """Normalize a drive-relative path and reject malformed ones."""
    if value is None:
        return None
    text = str(value)
    if "\\" in text or any(ord(char) < 32 for char in text):
        raise ValueError(f"{name} must be a '/'-separated drive path")
    segments = text.strip("/").split("/") if text.strip("/") else []
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError(f"{name} must not contain empty, '.' or '..' segments")
    return normalize_drive_path(text) or None


@cache
def supported_extensions() -> frozenset[str]:
    """Return every file extension the default parser families can read."""
    from harborrag_adapters.parsers.factory import HarborParserFactory

    registry = HarborParserFactory().create_registry()
    return frozenset(
        extension.lower() for parser in registry.families() for extension in parser.extensions
    )


def validate_extensions(allowed: set[str]) -> None:
    """Reject an empty allowlist and extensions no parser can extract."""
    if not allowed:
        raise ValueError("allowed_extensions must list at least one extension")
    unknown = sorted(allowed - supported_extensions())
    if unknown:
        raise ValueError(f"allowed_extensions has unsupported extensions: {', '.join(unknown)}")


def validate_scope(
    *,
    item_ids: list[str] | None,
    crawl_all_drives: bool,
    drive_selected: bool,
    root_path: str | None,
) -> None:
    """Reject empty or contradictory crawl scopes."""
    if item_ids is not None and not item_ids:
        raise ValueError("item_ids must list at least one item when set")
    if crawl_all_drives and (drive_selected or root_path or item_ids):
        raise ValueError(
            "crawl_all_drives covers the whole site; do not combine it with "
            "drive_id, drive_name, root_path, or item_ids"
        )
    if item_ids and root_path:
        raise ValueError("item_ids and root_path are separate scopes; set only one")


def normalize_allowlist(
    name: str,
    values: Iterable[str],
    *,
    allow_urls: bool = True,
) -> set[str]:
    """Normalize tenant or site allowlist entries for case-insensitive matching."""
    if isinstance(values, str):
        values = [values]
    normalized = set()
    for value in values:
        entry = str(value).strip()
        if not entry or any(char.isspace() for char in entry):
            raise ValueError(f"{name} entries must be non-empty identifiers")
        if "://" in entry:
            if not allow_urls:
                raise ValueError(f"{name} entries must be IDs, not URLs")
            validate_site_url(entry)
        normalized.add(normalize_identifier(entry))
    if not normalized:
        raise ValueError(f"{name} must list at least one entry when set")
    return normalized


def normalize_identifier(value: str) -> str:
    """Normalize a site ID, tenant ID, or site URL for allowlist comparison."""
    return value.strip().rstrip("/").casefold()


def normalize_path_prefixes(values: Iterable[str]) -> list[str]:
    """Normalize drive path prefixes; an empty prefix (``/``) is the drive root."""
    if isinstance(values, str):
        values = [values]
    prefixes = []
    for value in values:
        normalized = validate_drive_path("allowed_path_prefixes", str(value))
        prefixes.append((normalized or "").casefold())
    if not prefixes:
        raise ValueError("allowed_path_prefixes must list at least one prefix when set")
    return prefixes
