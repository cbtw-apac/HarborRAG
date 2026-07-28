"""Metadata and provenance helpers shared by parser families."""

from __future__ import annotations

from typing import Any

from harborrag_core.domain.parser import ParseInput


def parse_metadata(value: ParseInput, **extra: Any) -> dict[str, Any]:
    metadata = {
        **value.metadata,
        "filename": value.filename,
        "content_type": value.content_type,
        **extra,
    }
    return {key: item for key, item in metadata.items() if item is not None}
