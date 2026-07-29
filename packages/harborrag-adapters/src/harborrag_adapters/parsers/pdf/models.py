"""PDF-family result models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from harborrag_core.domain.element import DocumentElement


@dataclass(slots=True)
class PDFParseResult:
    """Normalized provider output evaluated by the PDF family workflow."""

    content: str
    engine: str
    quality_score: float | None = None
    elements: list[DocumentElement] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    raw: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.quality_score is not None and not 0 <= self.quality_score <= 1:
            raise ValueError("PDF quality_score must be between 0 and 1")

    def has_content(self, min_chars: int) -> bool:
        return len(self.content.strip()) >= min_chars


__all__ = ["PDFParseResult"]
