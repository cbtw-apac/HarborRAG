from __future__ import annotations
from dataclasses import field
from enum import Enum
from typing import Any

from pathlib import Path
from harborrag_core.domain.element import DocumentElement

class ParserFormatSupport(Enum):
    PDF = "pdf"
    PPTX = "pptx"
    DOCX = "docx"
    EXCEL = "excel"
    IMAGE = "image"
    HTML = "html"
    JSON = "json"
    MARKDOWN = "markdown"

class ParseInput:
    def __init__(self, path: str, content: bytes, filename: str, content_type: str):
        self.path = path
        self.content = content
        self.filename = filename
        self.content_type = content_type
    
    def __post_init__(self) -> None:
        if self.path is None and self.content is None:
            raise ValueError("ParseInput requires either `path` or `content`")
        if self.path is not None:
            self.path = Path(self.path)
        if self.filename is None and self.path is not None:
            self.filename = self.path.name
            
    def __repr__(self) -> str:
        if self.path:
            return f"<ParseInput path={self.path!r} content_type={self.content_type!r}>"
        else:
            return f"<ParseInput filename={self.filename!r} content_type={self.content_type!r}>"
    
    def __str__(self) -> str:
        if self.path:
            return f"ParseInput(path={self.path}, content_type={self.content_type})"
        else:
            return f"ParseInput(filename={self.filename}, content_type={self.content_type})"
    
    def _check_format_support(self, supported_formats: list[ParserFormatSupport]) -> bool:
        """Check if the input format is supported by the parser."""
        for fmt in supported_formats:
            if fmt.value in self.content_type or self.filename.endswith(f".{fmt.value}"):
                return True
        return False
    
    def is_supported(self, supported_formats: list[ParserFormatSupport]) -> bool:
        """Public method to check if the input format is supported."""
        return self._check_format_support(supported_formats)
        


class ParsedDocument:
    """What a BaseParser.parse() call produces.
    The `content` is the main text extracted from the document, while
    `elements` is a structured representation of the document's content, if the parser supports that. 
    `raw` is a catch-all for any additional data the parser wants to return.  
    """

    content: str = field(default="", metadata={"description": "The extracted text content of the document"})
    parser_name: str
    parser_version: str
    elements: list[DocumentElement] | None = field(default = None)
    warnings: list[str] | None = None
    raw: dict[str, Any] | None = None