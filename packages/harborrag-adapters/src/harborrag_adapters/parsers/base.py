from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from harborrag_adapters.documents import ParsedDocument

    from .types import ParseInput


class BaseParser(ABC):
    """
    Base class every format parser (DocxParser, XlsxParser, PyPdfParser, ImageParser, ...) implements.
    """

    parser_engine: ClassVar[str] = ""
    parser_version: ClassVar[str] = "1.0.0"

    suffixes: ClassVar[frozenset[str]] = frozenset()
    content_types: ClassVar[frozenset[str]] = frozenset()

    def __new__(cls, *args: Any, **kwargs: Any) -> "BaseParser":
        if not cls.parser_engine:
            raise TypeError(f"{cls.__name__} must set a non-empty `parser_engine` class attribute")
        if not hasattr(cls, "capabilities") or cls.capabilities is None:
            raise TypeError(f"{cls.__name__} must set a `capabilities` class attribute")
        return super().__new__(cls)

    @staticmethod
    def suffix_of(input: "ParseInput") -> str:
        """Lowercased filename suffix (e.g. ".pdf"), from file_name if
        set, otherwise derived from path. Used by the default can_parse()
        below; also available to subclasses overriding can_parse() with
        custom logic that still wants this without repeating it inline.
        """
        return Path(input.file_name or str(input.path or "")).suffix.lower()

    def can_parse(self, input: "ParseInput") -> bool:
        """True if this parser can handle `input`.

        Default: matches on `suffixes`/`content_types`. Override for
        anything more specific.
        """
        return (
            self.suffix_of(input) in self.suffixes
            or (input.content_type or "") in self.content_types
        )

    @abstractmethod
    def parse(self, input: "ParseInput") -> "ParsedDocument":
        """Extract content from `input`, returning a ParsedDocument.

        Should raise `harborrag_adapters.parsers.exceptions.ParseError`
        (not a bare Exception) for expected failure modes -- missing
        optional dependency, malformed input -- so callers (in particular
        ParserEngine) can distinguish "this format genuinely can't be
        parsed" from an unexpected bug worth surfacing differently.
        """
        raise NotImplementedError
    
    @abstractmethod
    def batch_parse(self, input: list["ParseInput"]) -> list["ParsedDocument"]:
        """
        Extract content from `input`, returning a list of ParsedDocument.
        """
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} parser_engine={self.parser_engine!r} version={self.parser_version!r}>"