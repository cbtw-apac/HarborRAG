from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class SourceRecord:
    """Lightweight reference discovered by a connector before full loading."""

    id: str
    source_type: str
    locator: str
    metadata: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime | None = None
    checksum: str | None = None
