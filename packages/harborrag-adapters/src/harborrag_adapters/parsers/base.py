from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Generic, TypeVar

from harborrag_core.domain.parser import ParseInput

from .utils import input_content_type, input_suffix, normalize_suffix, parse_metadata


ParserInput = TypeVar("ParserInput")
ParserOutput = TypeVar("ParserOutput")


class BaseParser(ABC, Generic[ParserInput, ParserOutput]):
    """Base contract for document parsers.

    A parser turns one raw document or parse input into one parsed document. Concrete
    parsers only need to implement :meth:`parse`; the default :meth:`batch_parse`
    keeps small parsers simple while still giving heavier implementations a hook for
    optimized batching.

    Format routing is intentionally metadata-only. Subclasses advertise supported
    filename suffixes and/or MIME content types, and may override :meth:`can_parse`
    when they need deeper inspection.
    """

    parser_name: ClassVar[str] = "base"
    parser_engine: ClassVar[str | None] = None
    parser_version: ClassVar[str | None] = "1.0.0"

    suffixes: ClassVar[frozenset[str]] = frozenset()
    content_types: ClassVar[frozenset[str]] = frozenset()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Normalize advertised route keys as soon as a parser class is defined."""
        super().__init_subclass__(**kwargs)
        cls.suffixes = frozenset(normalize_suffix(value) for value in cls.suffixes)
        cls.content_types = frozenset(
            value.lower().strip() for value in cls.content_types if value.strip()
        )

    @staticmethod
    def suffix_of(input: Any) -> str:
        """Return the lowercased filename suffix for an input, if one is known.

        The codebase has used both ``filename`` and ``file_name`` on parse inputs,
        while raw connector outputs may only have a path-like source field. This
        helper accepts those common shapes so parser implementations do not have to
        repeat compatibility glue.
        """
        return input_suffix(input)

    @staticmethod
    def content_type_of(input: Any) -> str:
        """Return a normalized MIME content type for an input, if one is known."""
        return input_content_type(input)

    @property
    def name(self) -> str:
        """Stable parser identifier used in parsed document provenance."""
        return self.parser_name

    def coerce_input(self, input: Any) -> ParseInput:
        """Convert a raw-doc-like object into the parser input contract."""
        return ParseInput.coerce(input)

    def metadata_for(self, input: ParseInput, **extra: Any) -> dict[str, Any]:
        """Build common parser metadata without repeating boilerplate."""
        return parse_metadata(input, **extra)

    def can_parse(self, input: ParserInput) -> bool:
        """Return whether this parser advertises support for ``input``."""
        suffix = self.suffix_of(input)
        content_type = self.content_type_of(input)
        return (bool(suffix) and suffix in self.suffixes) or (
            bool(content_type) and content_type in self.content_types
        )

    @abstractmethod
    def parse(self, input: ParserInput) -> ParserOutput:
        """Extract content from ``input`` and return a parsed document.

        Parsers should raise a parser-specific expected exception for unsupported,
        malformed, or dependency-limited inputs, and let unexpected bugs propagate.
        """
        raise NotImplementedError

    def batch_parse(self, inputs: list[ParserInput]) -> list[ParserOutput]:
        """Parse inputs in order using :meth:`parse`.

        Subclasses can override this when a backend supports true batch work.
        """
        return [self.parse(input) for input in inputs]

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} name={self.name!r} "
            f"engine={self.parser_engine!r} version={self.parser_version!r}>"
        )
