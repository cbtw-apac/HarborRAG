from __future__ import annotations

from html.parser import HTMLParser as StdlibHTMLParser
from pathlib import Path
from typing import Any

from harborrag_core.domain.parser import ParseInput

from .parser_logging import get_parser_logger, input_label


parser_logger = get_parser_logger("utils")


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
    """Build shared parsed-document metadata from input and parser extras."""
    values = {
        "filename": input.filename,
        "content_type": input.content_type,
        **input.metadata,
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
        return compact_text("\n".join(parser.parts))

    soup = BeautifulSoup(html, "html.parser")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return compact_text(soup.get_text(separator="\n", strip=True))


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
