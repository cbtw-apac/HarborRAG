from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Chunk:
    id: str
    document_id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
