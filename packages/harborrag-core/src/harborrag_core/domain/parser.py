from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .element import DocumentElement

_MAX_PATH_STRING_LENGTH = 260


def _has_bom(data: bytes, encoding: str) -> bool:
    """Return whether ``data`` starts with the byte-order mark for ``encoding``."""
    if encoding == "utf-8-sig":
        return data.startswith(b"\xef\xbb\xbf")
    if encoding == "utf-16":
        return data.startswith((b"\xff\xfe", b"\xfe\xff"))
    return False


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

    @classmethod
    def coerce(cls, input: Any, *, allow_path_strings: bool = False) -> ParseInput:
        if isinstance(input, cls):
            return input

        if isinstance(input, bytes):
            return cls(content=input)

        if isinstance(input, Path):
            return cls(path=input)

        if isinstance(input, str):
            # A bare string is ALWAYS treated as document content. Auto-promoting
            # a string to a filesystem path (based on os.path.exists) is an
            # arbitrary-file-read and non-determinism hazard when the string
            # originates from untrusted document content, so path reads must be
            # requested explicitly via a Path/ParseInput or allow_path_strings.
            if allow_path_strings:
                path = cls._path_from_string(input)
                if path is not None:
                    return cls(path=path)
            return cls(content=input)

        text_method = getattr(input, "text", None)
        content = getattr(input, "content", None)
        if content is None and callable(text_method):
            content = text_method()

        metadata = getattr(input, "metadata", {}) or {}
        return cls(
            path=getattr(input, "path", None),
            content=content,
            filename=(
                getattr(input, "filename", None)
                or getattr(input, "file_name", None)
                or cls._name_from(getattr(input, "source", None))
                or cls._name_from(getattr(input, "source_id", None))
            ),
            content_type=getattr(input, "content_type", None),
            metadata=dict(metadata),
        )

    @property
    def file_name(self) -> str | None:
        return self.filename

    @property
    def suffix(self) -> str:
        if self.filename:
            return Path(self.filename).suffix.lower()
        if self.path:
            return Path(self.path).suffix.lower()
        return ""

    def read_bytes(self) -> bytes:
        if isinstance(self.content, bytes):
            return self.content
        if isinstance(self.content, str):
            return self.content.encode("utf-8")
        if self.path is not None:
            return Path(self.path).read_bytes()
        raise ValueError("ParseInput has no readable bytes")

    def read_text(self, encoding: str | None = None) -> str:
        if isinstance(self.content, str):
            return self.content

        data = self.read_bytes()
        if encoding:
            return data.decode(encoding)

        # UTF variants with a BOM are unambiguous, so try them first.
        for candidate in ("utf-8-sig", "utf-16"):
            if _has_bom(data, candidate):
                try:
                    return data.decode(candidate)
                except UnicodeDecodeError:
                    break

        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            pass

        # Confidence-based detection avoids the cp1252 trap where almost any
        # byte sequence "successfully" decodes into mojibake with no signal.
        try:
            from charset_normalizer import from_bytes

            best = from_bytes(data).best()
            if best is not None:
                return str(best)
        except ImportError:
            pass

        # No encoding could be determined with any confidence. Raising here
        # (rather than silently substituting replacement characters) lets
        # callers surface this as a parse failure instead of returning
        # corrupted content that looks like it decoded successfully.
        raise UnicodeDecodeError(
            "utf-8", data, 0, len(data), "no encoding detected with confidence"
        )

    def is_supported(self, supported_formats: Iterable[ParserFormat | str]) -> bool:
        suffix = self.suffix.lstrip(".")
        content_type = (self.content_type or "").lower()
        for format_ in supported_formats:
            value = format_.value if isinstance(format_, ParserFormat) else str(format_)
            if value.lower() == suffix or value.lower() in content_type:
                return True
        return False

    def __repr__(self) -> str:
        where = f"path={self.path!r}" if self.path else f"filename={self.filename!r}"
        return f"<ParseInput {where} content_type={self.content_type!r}>"

    @staticmethod
    def _name_from(value: Any) -> str | None:
        if not value:
            return None
        name = Path(str(value)).name
        return name or None

    @staticmethod
    def _path_from_string(value: str) -> Path | None:
        if "\n" in value or len(value) >= _MAX_PATH_STRING_LENGTH:
            return None
        try:
            path = Path(value)
        except OSError:
            return None
        return path if path.exists() else None


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
