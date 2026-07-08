from __future__ import annotations

from typing import Any, Iterable, NamedTuple

from harborrag_core.domain.parser import ParsedDocument, ParseInput

from .base import BaseParser
from .ebook import EpubParser
from .exceptions import UnsupportedFormatError
from .html_engine import HtmlParser
from .image import ImageParser
from .markdown import MarkdownParser
from .office import DocxParser, ExcelParser, PptxParser
from .parser_logging import get_parser_logger, input_label, parser_log_extra
from .pdf_engine import PdfParser
from .structured import CsvParser, JsonParser
from .text import TextParser


parser_logger = get_parser_logger("registry")


class _ParserRoute(NamedTuple):
    """Resolved parser route and the key that selected it."""

    parser: BaseParser[ParseInput, ParsedDocument]
    kind: str
    key: str


class HarborParser:
    """Registry and factory for HarborRAG parser adapters.

    The registry indexes parsers by stable name, filename suffix, and content
    type. Routing is intentionally strict: conflicting suffix and MIME matches
    fail instead of silently choosing the wrong parser.
    """

    def __init__(
        self,
        parsers: Iterable[BaseParser[ParseInput, ParsedDocument]] | None = None,
    ) -> None:
        self.parsers: list[BaseParser[ParseInput, ParsedDocument]] = []
        self._by_name: dict[str, BaseParser[ParseInput, ParsedDocument]] = {}
        self._by_suffix: dict[str, BaseParser[ParseInput, ParsedDocument]] = {}
        self._by_content_type: dict[str, BaseParser[ParseInput, ParsedDocument]] = {}

        initial = self.default_parsers() if parsers is None else parsers
        for parser in initial:
            self.register(parser)

    @staticmethod
    def default_parsers() -> list[BaseParser[ParseInput, ParsedDocument]]:
        """Return the default parser stack in route-priority order."""
        return [
            PptxParser(),
            DocxParser(),
            ExcelParser(),
            PdfParser(),
            CsvParser(),
            ImageParser(),
            HtmlParser(),
            EpubParser(),
            JsonParser(),
            MarkdownParser(),
            TextParser(),
        ]

    def register(
        self,
        parser: BaseParser[ParseInput, ParsedDocument],
        *,
        replace: bool = False,
    ) -> None:
        """Register a parser and all of its advertised route keys."""
        if parser.name in self._by_name:
            if not replace:
                raise ValueError(f"Parser {parser.name!r} is already registered.")
            parser_logger.info(
                "Replacing parser %s",
                parser.name,
                extra=parser_log_extra(parser_name=parser.name),
            )
            self.unregister(parser.name)

        self._register_key(self._by_name, parser.name, parser, replace=replace)
        for suffix in parser.suffixes:
            self._register_key(self._by_suffix, suffix, parser, replace=replace)
        for content_type in parser.content_types:
            self._register_key(
                self._by_content_type,
                content_type,
                parser,
                replace=replace,
            )

        self.parsers.append(parser)
        parser_logger.debug(
            "Registered parser %s suffixes=%s content_types=%s",
            parser.name,
            sorted(parser.suffixes),
            sorted(parser.content_types),
            extra=parser_log_extra(
                parser_name=parser.name,
                parser_engine=parser.parser_engine,
                suffixes=sorted(parser.suffixes),
                content_types=sorted(parser.content_types),
            ),
        )

    def unregister(self, name: str) -> None:
        """Remove a parser by name from every route index."""
        try:
            parser = self._by_name.pop(name)
        except KeyError as exc:
            raise ValueError(f"Unknown parser: {name}") from exc

        for suffix in parser.suffixes:
            if self._by_suffix.get(suffix) is parser:
                del self._by_suffix[suffix]
        for content_type in parser.content_types:
            if self._by_content_type.get(content_type) is parser:
                del self._by_content_type[content_type]

        self.parsers = [
            registered for registered in self.parsers if registered is not parser
        ]
        parser_logger.debug(
            "Unregistered parser %s",
            name,
            extra=parser_log_extra(parser_name=name),
        )

    def create(self, name: str) -> BaseParser[ParseInput, ParsedDocument]:
        """Return a registered parser by stable parser name."""
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise ValueError(f"Unknown parser: {name}") from exc

    def parse(self, input: Any) -> ParsedDocument:
        """Coerce input, resolve a parser route, and parse one document."""
        parse_input = ParseInput.coerce(input)
        route = self._route_for(parse_input)
        if route is not None:
            parser_logger.debug(
                "Parsing %s with parser %s matched_by=%s key=%s",
                input_label(parse_input),
                route.parser.name,
                route.kind,
                route.key,
                extra=parser_log_extra(
                    input=parse_input,
                    parser_name=route.parser.name,
                    parser_engine=route.parser.parser_engine,
                    route_kind=route.kind,
                    route_key=route.key,
                ),
            )
            document = route.parser.parse(parse_input)
            parser_logger.debug(
                "Parsed %s with parser %s content_chars=%d elements=%d",
                input_label(parse_input),
                route.parser.name,
                len(document.content),
                len(document.elements or []),
                extra=parser_log_extra(
                    input=parse_input,
                    parser_name=route.parser.name,
                    parser_engine=route.parser.parser_engine,
                    route_kind=route.kind,
                    route_key=route.key,
                    content_chars=len(document.content),
                    elements=len(document.elements or []),
                ),
            )
            return document

        suffix = parse_input.suffix or "<none>"
        content_type = parse_input.content_type or "<none>"
        detail = f" suffix={suffix!r} content_type={content_type!r}"
        parser_logger.warning(
            "No parser registered for %s suffix=%s content_type=%s",
            input_label(parse_input),
            suffix,
            content_type,
            extra=parser_log_extra(input=parse_input),
        )
        raise UnsupportedFormatError(f"No parser registered for input with{detail}.")

    def parse_many(self, inputs: Iterable[Any]) -> list[ParsedDocument]:
        """Parse inputs sequentially with the same route rules as ``parse``."""
        return [self.parse(input) for input in inputs]

    def parser_for(
        self, input: Any
    ) -> BaseParser[ParseInput, ParsedDocument] | None:
        """Return the parser that would handle input, without parsing it."""
        route = self._route_for(ParseInput.coerce(input))
        return route.parser if route is not None else None

    def _route_for(self, parse_input: ParseInput) -> _ParserRoute | None:
        """Resolve suffix/content-type routes and reject ambiguous matches."""
        suffix_route = None
        if parse_input.suffix in self._by_suffix:
            suffix_route = _ParserRoute(
                parser=self._by_suffix[parse_input.suffix],
                kind="suffix",
                key=parse_input.suffix,
            )

        content_type = BaseParser.content_type_of(parse_input)
        content_type_route = None
        if content_type in self._by_content_type:
            content_type_route = _ParserRoute(
                parser=self._by_content_type[content_type],
                kind="content_type",
                key=content_type,
            )

        if (
            suffix_route is not None
            and content_type_route is not None
            and suffix_route.parser is not content_type_route.parser
        ):
            parser_logger.warning(
                "Conflicting parser routes for %s suffix=%s parser=%s "
                "content_type=%s parser=%s",
                input_label(parse_input),
                suffix_route.key,
                suffix_route.parser.name,
                content_type_route.key,
                content_type_route.parser.name,
                extra=parser_log_extra(
                    input=parse_input,
                    route_kind="conflict",
                    suffix_parser=suffix_route.parser.name,
                    content_type_parser=content_type_route.parser.name,
                ),
            )
            raise UnsupportedFormatError(
                "Conflicting parser routes for input: "
                f"suffix {suffix_route.key!r} maps to {suffix_route.parser.name!r}, "
                f"content type {content_type_route.key!r} maps to "
                f"{content_type_route.parser.name!r}."
            )

        return suffix_route or content_type_route

    @staticmethod
    def _register_key(
        index: dict[str, BaseParser[ParseInput, ParsedDocument]],
        key: str,
        parser: BaseParser[ParseInput, ParsedDocument],
        *,
        replace: bool,
    ) -> None:
        """Insert one route key while protecting existing parser ownership."""
        existing = index.get(key)
        if existing is not None and existing.name != parser.name and not replace:
            parser_logger.warning(
                "Refusing parser route override key=%s existing=%s new=%s",
                key,
                existing.name,
                parser.name,
                extra=parser_log_extra(
                    parser_name=parser.name,
                    route_key=key,
                    existing_parser=existing.name,
                ),
            )
            raise ValueError(
                f"Parser route {key!r} is already registered to {existing.name!r}; "
                "pass replace=True to override it."
            )
        index[key] = parser
