from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ElementType = Literal["heading", "paragraph", "table", "image", "code", "metadata"]


@dataclass(slots=True)
class DocumentElement:
    id: str
    type: ElementType
    content: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
