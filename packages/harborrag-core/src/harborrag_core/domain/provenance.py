from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True, kw_only=True)
class DocumentProvenance:
    """Describe where a normalized document came from and who may access it."""

    source: str
    record_id: str | None = None
    url: str | None = None
    author: str | None = None
    checksum: str | None = None
    permissions: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    tags: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
