"""Contracts shared by parser families and their internal engine adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar

from harborrag_adapters.parsers.common.metadata import parse_metadata
from harborrag_adapters.parsers.common.mime import (
    input_content_type,
    input_suffix,
    normalize_suffix,
)
from harborrag_adapters.parsers.common.models import ParseRequest, ParseResult
from harborrag_adapters.parsers.common.resources import coerce_parse_input
from harborrag_core.domain.parser import ParsedDocument, ParseInput


class HarborParser(ABC):
    """Complete parser-family contract exposed through the root registry.

    Family parsers own routing among their engines, quality policy,
    normalization, and fallback. The synchronous ``parse_input`` method is a
    compatibility boundary for current ingestion callers; new integrations use
    the asynchronous ``ParseRequest`` contract.
    """

    parser_name: ClassVar[str]
    extensions: frozenset[str] = frozenset()
    mime_types: frozenset[str] = frozenset()

    @property
    def name(self) -> str:
        return self.parser_name

    @abstractmethod
    async def parse(self, request: ParseRequest) -> ParseResult:
        raise NotImplementedError

    @abstractmethod
    def supports(self, source: Path, mime_type: str | None = None) -> bool:
        raise NotImplementedError

    @abstractmethod
    def parse_input(self, input: ParseInput) -> ParsedDocument:
        raise NotImplementedError


class HarborParserEngine[ParserInput, ParserOutput](ABC):
    """Internal base for synchronous provider adapters.

    This class contains only mechanics shared by multiple document families:
    input coercion, route metadata, provenance, and ordered batch execution.
    Family-specific engine contracts derive from it and add their own concepts.
    """

    parser_name: ClassVar[str] = "base"
    parser_engine: ClassVar[str | None] = None
    parser_version: ClassVar[str | None] = "1.0.0"

    suffixes: ClassVar[frozenset[str]] = frozenset()
    content_types: ClassVar[frozenset[str]] = frozenset()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls.suffixes = frozenset(normalize_suffix(value) for value in cls.suffixes)
        cls.content_types = frozenset(
            value.lower().strip() for value in cls.content_types if value.strip()
        )

    @staticmethod
    def suffix_of(input: Any) -> str:
        return input_suffix(input)

    @staticmethod
    def content_type_of(input: Any) -> str:
        return input_content_type(input)

    @property
    def name(self) -> str:
        return self.parser_name

    def coerce_input(self, input: Any) -> ParseInput:
        return coerce_parse_input(input)

    def metadata_for(self, input: ParseInput, **extra: Any) -> dict[str, Any]:
        return parse_metadata(input, **extra)

    def can_parse(self, input: ParserInput) -> bool:
        suffix = self.suffix_of(input)
        content_type = self.content_type_of(input)
        return (bool(suffix) and suffix in self.suffixes) or (
            bool(content_type) and content_type in self.content_types
        )

    @abstractmethod
    def parse(self, input: ParserInput) -> ParserOutput:
        raise NotImplementedError

    def batch_parse(self, inputs: list[ParserInput]) -> list[ParserOutput]:
        return [self.parse(input) for input in inputs]

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} name={self.name!r} "
            f"engine={self.parser_engine!r} version={self.parser_version!r}>"
        )


# Transitional import compatibility for downstream extensions. New engines
# should derive from their family contract instead of this alias.
BaseParser = HarborParserEngine

__all__ = ["HarborParser", "HarborParserEngine"]
