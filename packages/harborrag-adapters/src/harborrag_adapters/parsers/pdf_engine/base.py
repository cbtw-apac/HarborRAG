from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParseInput


@dataclass(slots=True)
class PdfParseResult:
    """Normalized result returned by every PDF backend."""

    content: str
    engine: str
    elements: list[DocumentElement] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    raw: dict[str, Any] | None = None

    def has_content(self, min_chars: int) -> bool:
        """Return whether this backend produced enough text to accept."""

        return len(self.content.strip()) >= min_chars


class PdfBackend(ABC):
    """Abstract contract for pluggable PDF extraction backends."""

    name: ClassVar[str]

    @abstractmethod
    def parse(self, input: ParseInput) -> PdfParseResult:
        """Extract content from a PDF ParseInput or raise ParseError/ImportError."""

        raise NotImplementedError
