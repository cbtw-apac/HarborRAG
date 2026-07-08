from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class RawDocument:
    """Raw connector payload before parser and normalizer stages."""

    id: str
    source: str
    content: str | bytes
    content_type: str
    metadata: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] | None = None

    def text(self, encoding: str = "utf-8") -> str:
        if isinstance(self.content, str):
            return self.content
        return self.content.decode(encoding, errors="replace")
