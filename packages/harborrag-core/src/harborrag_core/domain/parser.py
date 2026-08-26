from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .element import DocumentElement


class ParserFormat(StrEnum):
    PPTX = "pptx"
    DOCX = "docx"
    EXCEL = "excel"
    CSV = "csv"
    IMAGE = "image"
    HTML = "html"
    EPUB = "epub"
    JSON = "json"
    MARKDOWN = "markdown"
    PDF = "pdf"


@dataclass(slots=True)
class ParseInput:
    """Original document payload and routing metadata accepted by parser adapters.

    `content` here is the unparsed source payload. It may be bytes for binary
    formats or text for already-loaded text formats.
    """

    path: Path | str | None = None
    content: bytes | str | None = None
    filename: str | None = None
    content_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.path is not None:
            self.path = Path(self.path)
        if self.filename is None and self.path is not None:
            self.filename = self.path.name
        if self.path is None and self.content is None:
            raise ValueError("ParseInput requires either `path` or `content`")


@dataclass(slots=True)
class ParsedDocument:
    """Parser output shared across RAG stages."""

    content: str
    parser_name: str
    parser_version: str | None = "1.0.0"
    elements: list[DocumentElement] | None = None
    metadata: dict[str, Any] | None = None
    warnings: list[str] | None = None
    raw: dict[str, Any] | None = None
