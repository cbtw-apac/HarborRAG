from __future__ import annotations

from typing import ClassVar

from harborrag_adapters.parsers.common.normalization import html_to_text_with_engine
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
from harborrag_adapters.parsers.errors import ParseError
from harborrag_adapters.parsers.markup.base import HarborMarkupEngine
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument, ParseInput

parser_logger = get_parser_logger("html")


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
        try:
            html = read_parse_input_text(ParseInput(content=data))
        except UnicodeDecodeError as exc:
            raise ParseError(f"Could not decode HTML input: {exc}") from exc
        content, text_engine = html_to_text_with_engine(html)
        elements = (
            [
                DocumentElement(
                    id="html:0",
                    type="paragraph",
                    content=content,
                    metadata={"content_type": parse_input.content_type},
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
