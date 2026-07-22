from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class SplitBoundaryKind(StrEnum):
    """Describe the source boundary that produced a chunk or atomic unit."""

    DOCUMENT = "document"
    SECTION = "section"
    PARAGRAPH = "paragraph"
    SENTENCE = "sentence"
    TABLE = "table"
    TABLE_ROW = "table_row"
    CODE_SYMBOL = "code_symbol"
    CODE_BLOCK = "code_block"
    JSON_PATH = "json_path"
    LINE = "line"
    FORCED = "forced"


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """A framework-neutral location in normalized source content.

    Offsets are half-open. Line and page values are inclusive and may use the
    source format's native numbering (normally one-based).
    """

    start_offset: int | None = None
    end_offset: int | None = None
    start_line: int | None = None
    end_line: int | None = None
    page_start: int | None = None
    page_end: int | None = None
    element_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self._validate_pair("offset", self.start_offset, self.end_offset, minimum=0)
        self._validate_pair("line", self.start_line, self.end_line, minimum=1)
        self._validate_pair("page", self.page_start, self.page_end, minimum=0)
        if any(not value.strip() for value in self.element_ids):
            raise ValueError("source span element_ids must be non-empty")

    @staticmethod
    def _validate_pair(
        label: str,
        start: int | None,
        end: int | None,
        *,
        minimum: int,
    ) -> None:
        if (start is None) != (end is None):
            raise ValueError(f"source span {label} bounds must be provided together")
        if start is None or end is None:
            return
        if start < minimum or end < start:
            raise ValueError(f"invalid source span {label} bounds")


@dataclass(frozen=True, slots=True)
class TextSplit:
    """One HarborRAG-owned text candidate returned by a refiner."""

    content: str
    token_count: int
    source_span: SourceSpan | None = None
    boundary_kind: SplitBoundaryKind = SplitBoundaryKind.FORCED
    structural_path: tuple[str, ...] = ()
    prefix: str | None = None
    forced_split: bool = False

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("split content must not be empty")
        if self.token_count < 1:
            raise ValueError("split token_count must be positive")
        if any(not part.strip() for part in self.structural_path):
            raise ValueError("split structural_path parts must be non-empty")
        if self.prefix is not None and not self.prefix.strip():
            raise ValueError("split prefix must be non-empty when provided")


@dataclass(frozen=True, slots=True)
class TextRefinementRequest:
    """Ask a refiner to enforce one hard token limit on a text unit."""

    content: str
    maximum_tokens: int
    overlap_tokens: int = 0
    source_span: SourceSpan | None = None
    boundary_kind: SplitBoundaryKind = SplitBoundaryKind.PARAGRAPH
    structural_path: tuple[str, ...] = ()
    preserve_whitespace: bool = True
    separators: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.maximum_tokens < 1:
            raise ValueError("maximum_tokens must be positive")
        if not 0 <= self.overlap_tokens < self.maximum_tokens:
            raise ValueError("overlap_tokens must satisfy 0 <= overlap < maximum")
        if any(not part.strip() for part in self.structural_path):
            raise ValueError("structural_path parts must be non-empty")
        if self.separators is not None:
            if not self.separators or self.separators[-1] != "":
                raise ValueError(
                    "separators must be non-empty and end with an empty fallback"
                )
            if len(set(self.separators)) != len(self.separators):
                raise ValueError("separators must not contain duplicates")


@dataclass(frozen=True, slots=True)
class StructureSplitRequest:
    """Ask a format adapter to recover hierarchy from raw markup fallback text."""

    content: str
    source_span: SourceSpan | None = None


@dataclass(frozen=True, slots=True)
class JsonStructureSplitRequest:
    """Ask a format adapter to split a JSON object without flattening it."""

    value: Mapping[str, Any] | Sequence[Any]
    maximum_characters: int = 2000
    minimum_characters: int | None = None
    convert_lists: bool = True
    ensure_ascii: bool = False
    source_span: SourceSpan | None = None

    def __post_init__(self) -> None:
        if isinstance(self.value, (str, bytes, bytearray)):
            raise TypeError("JSON structure input must be an object or array")
        if self.maximum_characters < 1:
            raise ValueError("maximum_characters must be positive")
        if self.minimum_characters is not None and not (
            0 <= self.minimum_characters <= self.maximum_characters
        ):
            raise ValueError("minimum_characters must satisfy 0 <= minimum <= maximum")


@runtime_checkable
class TokenCounter(Protocol):
    """Count model-oriented tokens without exposing a provider tokenizer."""

    def count(self, text: str) -> int:
        """Return the number of tokens in ``text``."""


@runtime_checkable
class TextRefiner(Protocol):
    """Refine one oversized unit without owning document-level policy."""

    def split(self, request: TextRefinementRequest) -> tuple[TextSplit, ...]:
        """Return ordered splits that do not exceed the requested hard limit."""


@runtime_checkable
class StructureSplitter(Protocol):
    """Recover preferred structural units from raw format fallback text."""

    def split(self, request: StructureSplitRequest) -> tuple[TextSplit, ...]:
        """Return HarborRAG-owned structural splits."""


@runtime_checkable
class JsonStructureSplitter(Protocol):
    """Split JSON while retaining object and path structure."""

    def split(self, request: JsonStructureSplitRequest) -> tuple[TextSplit, ...]:
        """Return HarborRAG-owned JSON splits."""
