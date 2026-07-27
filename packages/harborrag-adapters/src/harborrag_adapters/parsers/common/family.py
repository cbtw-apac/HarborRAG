"""Reusable mechanics for families whose routing selects one local engine."""

from __future__ import annotations

from pathlib import Path
from time import perf_counter
from typing import ClassVar

from harborrag_adapters.parsers.common.base import HarborParser, HarborParserEngine
from harborrag_adapters.parsers.common.mime import input_content_type
from harborrag_adapters.parsers.common.models import ParserAttempt, ParseRequest, ParseResult
from harborrag_adapters.parsers.common.resources import (
    parse_input_suffix,
    request_to_parse_input,
)
from harborrag_adapters.parsers.errors import UnsupportedParserEngineError
from harborrag_core.domain.parser import ParsedDocument, ParseInput


class HarborSingleEngineFamilyParser(HarborParser):
    """Family workflow for formats resolved to exactly one provider engine.

    PDF deliberately does not derive from this class because its workflow may
    attempt multiple engines and evaluate output quality before accepting one.
    """

    parser_name: ClassVar[str]

    def __init__(
        self,
        router: SingleEngineRouter | tuple[HarborParserEngine[ParseInput, ParsedDocument], ...],
        normalizer: FamilyResultNormalizer | None = None,
    ) -> None:
        if isinstance(router, tuple):
            router = SingleEngineRouter(self.parser_name, router)
        self._router = router
        self._normalizer = normalizer or FamilyResultNormalizer(self.parser_name)
        self.extensions = router.extensions
        self.mime_types = router.mime_types

    @property
    def engines(self) -> tuple[HarborParserEngine[ParseInput, ParsedDocument], ...]:
        return self._router.engines

    async def parse(self, request: ParseRequest) -> ParseResult:
        parse_input = request_to_parse_input(request)
        engine = self._router.resolve(parse_input, request.engine)
        started = perf_counter()
        document = engine.parse(parse_input)
        duration_ms = (perf_counter() - started) * 1000
        return self._normalizer.normalize(
            document,
            engine_name=engine.name,
            attempts=[
                ParserAttempt(
                    engine=engine.name,
                    success=True,
                    duration_ms=duration_ms,
                    quality_score=1.0,
                )
            ],
        )

    def supports(self, source: Path, mime_type: str | None = None) -> bool:
        normalized_mime = (mime_type or "").partition(";")[0].strip().lower()
        return source.suffix.lower() in self.extensions or (
            bool(normalized_mime) and normalized_mime in self.mime_types
        )

    def parse_input(self, input: ParseInput) -> ParsedDocument:
        return self._router.resolve(input, None).parse(input)

    def engine(self, name: str) -> HarborParserEngine[ParseInput, ParsedDocument]:
        return self._router.get(name)


class SingleEngineRouter:
    """Resolve one family engine without leaking providers into the root registry."""

    def __init__(
        self,
        family: str,
        engines: tuple[HarborParserEngine[ParseInput, ParsedDocument], ...],
    ) -> None:
        if not engines:
            raise ValueError(f"{family} parser requires at least one engine")
        self._family = family
        self._engines = engines
        self._by_name: dict[str, HarborParserEngine[ParseInput, ParsedDocument]] = {}
        self._by_extension: dict[str, HarborParserEngine[ParseInput, ParsedDocument]] = {}
        self._by_mime_type: dict[str, HarborParserEngine[ParseInput, ParsedDocument]] = {}
        for engine in engines:
            self._register(engine)
        self.extensions = frozenset(self._by_extension)
        self.mime_types = frozenset(self._by_mime_type)

    @property
    def engines(self) -> tuple[HarborParserEngine[ParseInput, ParsedDocument], ...]:
        return self._engines

    def get(self, name: str) -> HarborParserEngine[ParseInput, ParsedDocument]:
        try:
            return self._by_name[name]
        except KeyError as error:
            raise UnsupportedParserEngineError(family=self._family, engine=name) from error

    def resolve(
        self,
        parse_input: ParseInput,
        explicit_engine: str | None,
    ) -> HarborParserEngine[ParseInput, ParsedDocument]:
        if explicit_engine:
            return self.get(explicit_engine)

        suffix_engine = self._by_extension.get(parse_input_suffix(parse_input))
        mime_engine = self._by_mime_type.get(input_content_type(parse_input))
        if suffix_engine is not None:
            return suffix_engine
        if mime_engine is not None:
            return mime_engine
        raise UnsupportedParserEngineError(
            family=self._family,
            engine=explicit_engine or "<unresolved>",
        )

    def _register(self, engine: HarborParserEngine[ParseInput, ParsedDocument]) -> None:
        if engine.name in self._by_name:
            raise ValueError(f"Duplicate {self._family} parser engine: {engine.name!r}")
        self._by_name[engine.name] = engine
        self._register_routes(self._by_extension, engine.suffixes, engine)
        self._register_routes(self._by_mime_type, engine.content_types, engine)

    def _register_routes(
        self,
        routes: dict[str, HarborParserEngine[ParseInput, ParsedDocument]],
        values: frozenset[str],
        engine: HarborParserEngine[ParseInput, ParsedDocument],
    ) -> None:
        for value in values:
            existing = routes.get(value)
            if existing is not None:
                raise ValueError(
                    f"Duplicate {self._family} parser route {value!r}: "
                    f"{existing.name!r} and {engine.name!r}"
                )
            routes[value] = engine


class FamilyResultNormalizer:
    """Convert a provider document into the stable cross-family result."""

    def __init__(self, parser_name: str) -> None:
        self._parser_name = parser_name

    def normalize(
        self,
        document: ParsedDocument,
        *,
        engine_name: str,
        attempts: list[ParserAttempt],
    ) -> ParseResult:
        result = ParseResult.from_parsed_document(
            document,
            engine_name=engine_name,
            attempts=attempts,
        )
        result.parser_name = self._parser_name
        return result
