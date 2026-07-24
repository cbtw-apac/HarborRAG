from __future__ import annotations

from pathlib import Path
from typing import Any

from harborrag_core.domain.parser import ParseInput

from .input_loading import parse_input_suffix


def normalize_suffix(value: str) -> str:
    """Normalize parser suffix declarations to lowercased dot-prefixed values."""
    value = value.strip().lower()
    if not value:
        return value
    return value if value.startswith(".") else f".{value}"


def input_suffix(value: Any) -> str:
    """Best-effort suffix extraction from parser inputs or raw documents."""
    if isinstance(value, ParseInput):
        return parse_input_suffix(value)
    for attribute in ("file_name", "filename", "path", "source", "source_id"):
        candidate = getattr(value, attribute, None)
        if candidate:
            suffix = Path(str(candidate)).suffix.lower()
            if suffix:
                return suffix
    return ""


def input_content_type(value: Any) -> str:
    """Normalize a MIME type and strip optional parameters."""
    content_type = getattr(value, "content_type", "") or ""
    return str(content_type).partition(";")[0].strip().lower()


def parse_metadata(value: ParseInput, **extra: Any) -> dict[str, Any]:
    """Build trusted parser provenance without repeating routing behavior."""
    metadata = {
        **value.metadata,
        "filename": value.filename,
        "content_type": value.content_type,
        **extra,
    }
    return {key: item for key, item in metadata.items() if item is not None}
