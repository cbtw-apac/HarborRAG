from __future__ import annotations

from pathlib import Path
from typing import Any

from harborrag_adapters.parsers.common.resources import parse_input_suffix
from harborrag_core.domain.parser import ParseInput


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
