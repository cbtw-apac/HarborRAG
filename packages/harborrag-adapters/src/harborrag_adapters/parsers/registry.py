"""Root registry that resolves document metadata to one parser family."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Literal, NamedTuple

from harborrag_adapters.parsers.common.base import HarborParser
from harborrag_adapters.parsers.common.models import ParseRequest, ParseResult
from harborrag_adapters.parsers.common.resources import (
    coerce_parse_input,
    parse_input_suffix,
)
from harborrag_adapters.parsers.common.utils import (
    get_parser_logger,
    input_label,
)
from harborrag_adapters.parsers.errors import (
    ParseError,
    UnsupportedFormatError,
    UnsupportedParserError,
)
from harborrag_core.domain.parser import ParsedDocument

ParserBuilder = Callable[[], HarborParser]
parser_logger = get_parser_logger("registry")

_GENERIC_MIME_TYPES = frozenset(
    {"", "text/plain", "application/octet-stream", "binary/octet-stream"}
)


class _FamilyRoute(NamedTuple):
    """Resolved parser family and the metadata key that selected it."""

    builder: ParserBuilder
    kind: str
    key: str


class HarborParserRegistry:
    """Map MIME types and extensions to complete parser families.

    Provider engines never appear in these indexes. Each resolved family owns
    its engine-selection and fallback policy behind the ``HarborParser``
    contract.
    """

    def __init__(self) -> None:
        """Initialize independent family routing indexes."""

        self._mime_types: dict[str, ParserBuilder] = {}
        self._extensions: dict[str, ParserBuilder] = {}
        self._families: dict[str, ParserBuilder] = {}
        self._family_routes: dict[str, tuple[frozenset[str], frozenset[str]]] = {}

    def register_mime_type(
        self,
        mime_type: str,
        builder: ParserBuilder,
        *,
        replace: bool = False,
    ) -> None:
        normalized = mime_type.partition(";")[0].strip().lower()
        self._register_route(
            self._mime_types,
            normalized,
            builder,
            replace=replace,
        )

    def register_extension(
        self,
        extension: str,
        builder: ParserBuilder,
        *,
        replace: bool = False,
    ) -> None:
        normalized = extension.lower().strip()
        normalized = normalized if normalized.startswith(".") else f".{normalized}"
        self._register_route(
            self._extensions,
            normalized,
            builder,
            replace=replace,
        )

    def register_family(
        self,
        parser: HarborParser,
        *,
        replace: bool = False,
    ) -> None:
        name = parser.parser_name
        if name in self._families and not replace:
            raise ValueError(f"Parser family {name!r} is already registered.")
        if name in self._families:
            self.unregister(name)

        def builder() -> HarborParser:
            return parser

        for extension in parser.extensions:
            self.register_extension(extension, builder, replace=replace)
        for mime_type in parser.mime_types:
            self.register_mime_type(mime_type, builder, replace=replace)
        self._families[name] = builder
        self._family_routes[name] = (parser.extensions, parser.mime_types)

    def unregister(self, name: str) -> None:
        try:
            builder = self._families.pop(name)
        except KeyError as error:
            raise ValueError(f"Unknown parser family: {name}") from error

        extensions, mime_types = self._family_routes.pop(name)
        for extension in extensions:
            if self._extensions.get(extension) is builder:
                del self._extensions[extension]
        for mime_type in mime_types:
            if self._mime_types.get(mime_type) is builder:
                del self._mime_types[mime_type]

    def resolve(
        self,
        filename: str | None,
        mime_type: str | None,
    ) -> HarborParser:
        route = self._route(filename, mime_type)
        if route is None:
            raise UnsupportedParserError(filename=filename, mime_type=mime_type)
        return route.builder()

    def create(self, name: str) -> HarborParser:
        try:
            return self._families[name]()
        except KeyError as error:
            raise ValueError(f"Unknown parser family: {name}") from error

    async def parse_request(self, request: ParseRequest) -> ParseResult:
        parser = (
            self.create(request.parser)
            if request.parser is not None
            else self.resolve(request.filename, request.mime_type)
        )
        return await parser.parse(request)

    def parse(self, source: Any) -> ParsedDocument:
        parse_input = coerce_parse_input(source)
        route = self._route(parse_input.filename, parse_input.content_type)
        if route is None:
            suffix = parse_input_suffix(parse_input) or "<none>"
            content_type = parse_input.content_type or "<none>"
            raise UnsupportedFormatError(
                "No parser family registered for input with "
                f"suffix={suffix!r} content_type={content_type!r}."
            )

        parser = route.builder()
        parser_logger.debug(
            "Parsing %s with %s via %s=%s",
            input_label(parse_input),
            parser.parser_name,
            route.kind,
            route.key,
        )
        try:
            document = parser.parse_input(parse_input)
        except ParseError:
            raise
        except Exception as error:  # noqa: BLE001
            parser_logger.error(
                "Unexpected parser family failure parser=%s input=%s exception_type=%s",
                parser.parser_name,
                input_label(parse_input),
                type(error).__name__,
            )
            raise
        parser_logger.debug(
            "Parsed %s with parser %s content_chars=%d elements=%d",
            input_label(parse_input),
            document.parser_name,
            len(document.content),
            len(document.elements or []),
        )
        return document

    def parse_many(
        self,
        sources: Iterable[Any],
        *,
        on_error: Literal["raise", "skip"] = "raise",
    ) -> list[ParsedDocument]:
        # Literal is static-only; validate here so a value threaded in from config
        # fails loudly instead of silently defaulting to "skip" below.
        if on_error not in ("raise", "skip"):
            raise ValueError(f"Unknown on_error policy: {on_error!r}")

        results: list[ParsedDocument] = []
        for index, source in enumerate(sources):
            document = self._parse_or_skip(source, index, on_error=on_error)
            if document is not None:
                results.append(document)
        return results

    def _parse_or_skip(
        self,
        source: Any,
        index: int,
        *,
        on_error: Literal["raise", "skip"],
    ) -> ParsedDocument | None:
        """Parse one input, returning None only when the caller opted into skipping."""

        try:
            return self.parse(source)
        except ParseError as error:
            if on_error == "raise":
                raise
            parser_logger.warning(
                "Skipping input %d after parse failure: %s",
                index,
                error,
            )
            return None

    def parser_for(self, source: Any) -> HarborParser | None:
        parse_input = coerce_parse_input(source)
        route = self._route(parse_input.filename, parse_input.content_type)
        return route.builder() if route is not None else None

    def families(self) -> tuple[HarborParser, ...]:
        return tuple(builder() for builder in self._families.values())

    def _route(
        self,
        filename: str | None,
        mime_type: str | None,
    ) -> _FamilyRoute | None:
        suffix = Path(filename).suffix.lower() if filename else ""
        extension_builder = self._extensions.get(suffix)
        extension_route = (
            _FamilyRoute(extension_builder, "extension", suffix)
            if extension_builder is not None
            else None
        )

        normalized_mime = (mime_type or "").partition(";")[0].strip().lower()
        mime_builder = self._mime_types.get(normalized_mime)
        mime_route = (
            _FamilyRoute(mime_builder, "mime_type", normalized_mime)
            if mime_builder is not None
            else None
        )

        if (
            extension_route is not None
            and mime_route is not None
            and extension_route.builder is not mime_route.builder
        ):
            if normalized_mime in _GENERIC_MIME_TYPES:
                return extension_route
            raise UnsupportedFormatError(
                "Conflicting parser-family routes for input: "
                f"extension {suffix!r} and MIME type {normalized_mime!r}."
            )
        return mime_route or extension_route

    @staticmethod
    def _register_route(
        index: dict[str, ParserBuilder],
        key: str,
        builder: ParserBuilder,
        *,
        replace: bool,
    ) -> None:
        if not key:
            raise ValueError("Parser route key cannot be empty")
        existing = index.get(key)
        if existing is not None and existing is not builder and not replace:
            raise ValueError(f"Parser route {key!r} is already registered.")
        index[key] = builder
