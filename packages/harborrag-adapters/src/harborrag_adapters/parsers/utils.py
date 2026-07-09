from __future__ import annotations

import zipfile
from contextlib import contextmanager
from html.parser import HTMLParser as StdlibHTMLParser
from io import BytesIO
from pathlib import Path
from typing import Any, Iterator

from harborrag_core.domain.parser import ParseInput

from .exceptions import ParseError
from .parser_logging import get_parser_logger, input_label


parser_logger = get_parser_logger("utils")

DEFAULT_MAX_INPUT_BYTES = 512 * 1024 * 1024  # 512 MiB raw input
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB total
MAX_ARCHIVE_COMPRESSION_RATIO = 200  # per-member uncompressed / compressed


@contextmanager
def wrap_parse_errors(engine: str) -> Iterator[None]:
    """Normalize third-party parsing failures into :class:`ParseError`.

    Concrete parsers wrap the library call so that malformed/corrupt/encrypted
    inputs surface as the documented expected exception (with the original
    preserved as ``__cause__``) instead of leaking a zoo of library-specific
    types (``BadZipFile``, ``RecursionError``, ``csv.Error`` …) that callers
    cannot catch to quarantine bad documents. Genuine programming errors are
    intentionally NOT swallowed.
    """
    try:
        yield
    except ParseError:
        raise
    except (
        zipfile.BadZipFile,
        ValueError,
        KeyError,
        OSError,
        RecursionError,
    ) as exc:
        raise ParseError(f"{engine} failed to parse input: {exc}") from exc


def guard_input_size(data: bytes, *, max_bytes: int = DEFAULT_MAX_INPUT_BYTES) -> bytes:
    """Reject oversized raw inputs before a parser materializes them further."""
    if len(data) > max_bytes:
        raise ParseError(
            f"Input size {len(data)} exceeds max_input_bytes {max_bytes}"
        )
    return data


def open_guarded_zip(data: bytes) -> zipfile.ZipFile:
    """Open a zip container after rejecting decompression-bomb shapes.

    EPUB/DOCX/PPTX/XLSX are all zip containers. Without member-count, total
    uncompressed-size, and per-member ratio checks a few-KB upload can inflate
    to gigabytes in memory. This validates the central directory *before* any
    member is read.
    """
    archive = zipfile.ZipFile(BytesIO(data))
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_MEMBERS:
        archive.close()
        raise ParseError(
            f"Archive has {len(infos)} members (max {MAX_ARCHIVE_MEMBERS})"
        )
    total = 0
    for info in infos:
        total += info.file_size
        if total > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            archive.close()
            raise ParseError(
                f"Archive uncompressed size exceeds "
                f"{MAX_ARCHIVE_UNCOMPRESSED_BYTES} bytes"
            )
        if info.compress_size > 0:
            ratio = info.file_size / info.compress_size
            if ratio > MAX_ARCHIVE_COMPRESSION_RATIO:
                archive.close()
                raise ParseError(
                    f"Archive member {info.filename!r} compression ratio "
                    f"{ratio:.0f} exceeds {MAX_ARCHIVE_COMPRESSION_RATIO}"
                )
    return archive


def normalize_suffix(value: str) -> str:
    """Normalize parser suffix declarations to lowercased dot-prefixed values."""
    value = value.strip().lower()
    if not value:
        return value
    return value if value.startswith(".") else f".{value}"


def input_suffix(input: Any) -> str:
    """Best-effort suffix extraction from parser inputs or raw documents."""
    if isinstance(input, ParseInput):
        return input.suffix

    for attribute in ("file_name", "filename", "path", "source", "source_id"):
        value = getattr(input, attribute, None)
        if value:
            suffix = Path(str(value)).suffix.lower()
            if suffix:
                return suffix
    return ""


def input_content_type(input: Any) -> str:
    """Normalize a MIME type and strip optional parameters."""
    content_type = getattr(input, "content_type", "") or ""
    return str(content_type).partition(";")[0].strip().lower()


def parse_metadata(input: ParseInput, **extra: Any) -> dict[str, Any]:
    """Build shared parsed-document metadata from input and parser extras.

    Document-supplied ``input.metadata`` is spread first so that the computed,
    trusted ``filename``/``content_type`` provenance cannot be spoofed by a
    document that carries its own ``filename`` key.
    """
    values = {
        **input.metadata,
        "filename": input.filename,
        "content_type": input.content_type,
        **extra,
    }
    return {key: value for key, value in values.items() if value is not None}


def compact_text(text: str) -> str:
    """Trim lines while preserving intentional paragraph breaks."""
    lines = [line.strip() for line in text.splitlines()]
    compact: list[str] = []
    for line in lines:
        if not line:
            if compact and compact[-1]:
                compact.append("")
            continue
        compact.append(line)
    return "\n".join(compact).strip()


def html_to_text(html: str | bytes) -> str:
    """Extract visible text from HTML using Beautiful Soup or stdlib fallback."""
    return html_to_text_with_engine(html)[0]


def html_to_text_with_engine(html: str | bytes) -> tuple[str, str]:
    """Extract visible text and report which backend produced it.

    Returning the actual backend lets parsers record honest provenance instead
    of always claiming BeautifulSoup, so content-hash dedup stays consistent
    across workers that have different optional dependencies installed.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        parser_logger.debug("BeautifulSoup is not installed; using stdlib HTML parser")
        parser = _FallbackHTMLTextParser()
        parser.feed(
            html.decode("utf-8", errors="replace")
            if isinstance(html, bytes)
            else html
        )
        return compact_text("\n".join(parser.parts)), "python/html.parser"

    soup = BeautifulSoup(html, "html.parser")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return (
        compact_text(soup.get_text(separator="\n", strip=True)),
        "beautifulsoup4/html.parser",
    )


class _FallbackHTMLTextParser(StdlibHTMLParser):
    """Small dependency-free visible-text extractor for HTML."""

    _SKIP_TAGS = {"script", "style", "noscript"}
    _BLOCK_TAGS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "p",
        "section",
        "td",
        "th",
        "tr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Track skipped and block-level tags while parsing HTML."""
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        if tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        """Close skipped/block-level tags while parsing HTML."""
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        if tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        """Append visible text when not inside skipped content."""
        if not self._skip_depth:
            stripped = data.strip()
            if stripped:
                self.parts.append(stripped)
