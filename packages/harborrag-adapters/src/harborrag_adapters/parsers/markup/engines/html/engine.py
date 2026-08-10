from __future__ import annotations

from typing import ClassVar

from harborrag_adapters.parsers.common.normalization import (
    _FallbackHTMLTextParser,
    compact_text,
    html_to_text_with_engine,
)
from harborrag_adapters.parsers.common.resources import (
    read_parse_input_bytes,
    read_parse_input_text,
)
from harborrag_adapters.parsers.common.utils import (
    get_parser_logger,
    input_label,
    parser_log_extra,
)
from harborrag_adapters.parsers.common.validation import guard_input_size
from harborrag_adapters.parsers.markup.base import HarborMarkupEngine
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument, ParseInput

parser_logger = get_parser_logger("html")


class _LinkCapturingFallbackParser(_FallbackHTMLTextParser):
    """Stdlib-only text extractor that also records anchor href/title/text.

    Subclasses the shared fallback parser instead of modifying it, since that
    base class is also used by the EPUB engine and the Jira connector's
    content builder for plain-text extraction alone.
    """

    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str | None]] = []
        self._link_stack: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        super().handle_starttag(tag, attrs)
        if tag == "a":
            attr_map = dict(attrs)
            href = attr_map.get("href")
            if href:
                self._link_stack.append({"href": href, "title": attr_map.get("title"), "text": ""})

    def handle_data(self, data: str) -> None:
        super().handle_data(data)
        if self._link_stack and not self._skip_depth:
            stripped = data.strip()
            if stripped:
                current = self._link_stack[-1]
                current["text"] = f"{current['text']} {stripped}".strip() if current["text"] else stripped

    def handle_endtag(self, tag: str) -> None:
        super().handle_endtag(tag)
        if tag == "a" and self._link_stack:
            self.links.append(self._link_stack.pop())


def _attr_str(value: object) -> str | None:
    """Normalize a bs4 attribute value (str, list, or None) to a plain string.

    bs4 returns a list for space/comma-separated multi-valued attributes
    (e.g. `class`); `href`/`title` are single-valued in practice, but the
    stub type is the same for every attribute, so this coerces defensively.
    """
    if value is None:
        return None
    if isinstance(value, list):
        return " ".join(str(item) for item in value) or None
    return str(value)


def _extract_links(html: str) -> list[dict[str, str | None]]:
    """Collect `<a href title>` metadata that plain-text extraction discards."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        parser = _LinkCapturingFallbackParser()
        parser.feed(html)
        parser.close()
        return parser.links

    soup = BeautifulSoup(html, "html.parser")
    links: list[dict[str, str | None]] = []
    for anchor in soup.find_all("a"):
        href = _attr_str(anchor.get("href"))
        if not href:
            continue
        links.append(
            {
                "href": href,
                "title": _attr_str(anchor.get("title")),
                "text": compact_text(anchor.get_text(separator=" ", strip=True)),
            }
        )
    return links


class HtmlMarkupEngine(HarborMarkupEngine):
    """Extract visible text from HTML and XHTML documents."""

    parser_name: ClassVar[str] = "html"
    parser_engine: ClassVar[str] = "beautifulsoup4/html.parser"
    suffixes: ClassVar[frozenset[str]] = frozenset({"html", "htm", "xhtml"})
    content_types: ClassVar[frozenset[str]] = frozenset({"text/html", "application/xhtml+xml"})

    def parse(self, input: ParseInput) -> ParsedDocument:
        """Strip markup and return a single paragraph element with visible content."""

        parse_input = self.coerce_input(input)
        parser_logger.debug(
            "Extracting HTML text from %s",
            input_label(parse_input),
            extra=parser_log_extra(
                input=parse_input,
                parser_name=self.parser_name,
                parser_engine=self.parser_engine,
            ),
        )
        data = guard_input_size(read_parse_input_bytes(parse_input))
        html = read_parse_input_text(ParseInput(content=data))
        content, text_engine = html_to_text_with_engine(html)
        links = _extract_links(html)
        elements = (
            [
                DocumentElement(
                    id="html:0",
                    type="paragraph",
                    content=content,
                    metadata={
                        "content_type": parse_input.content_type,
                        **({"links": links} if links else {}),
                    },
                )
            ]
            if content
            else []
        )
        parser_logger.info(
            "Parsed HTML %s text_engine=%s content_chars=%d elements=%d",
            input_label(parse_input),
            text_engine,
            len(content),
            len(elements),
            extra=parser_log_extra(
                input=parse_input,
                parser_name=self.parser_name,
                parser_engine=self.parser_engine,
                text_engine=text_engine,
                content_chars=len(content),
                elements=len(elements),
            ),
        )
        return ParsedDocument(
            content=content,
            elements=elements,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            metadata=self.metadata_for(parse_input, text_engine=text_engine),
            raw={"html": html},
        )


HtmlParser = HtmlMarkupEngine
