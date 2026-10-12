from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


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
    language: str | None = None

    def __post_init__(self) -> None:
        if self.language is not None:
            language = self.language.strip().lower()
            if not language:
                raise ValueError("language must be non-empty when provided")
            object.__setattr__(self, "language", language)
        if self.maximum_tokens < 1:
            raise ValueError("maximum_tokens must be positive")
        if not 0 <= self.overlap_tokens < self.maximum_tokens:
            raise ValueError("overlap_tokens must satisfy 0 <= overlap < maximum")
        if any(not part.strip() for part in self.structural_path):
            raise ValueError("structural_path parts must be non-empty")
        if self.separators is not None:
            if not self.separators or self.separators[-1] != "":
                raise ValueError("separators must be non-empty and end with an empty fallback")
            if len(set(self.separators)) != len(self.separators):
                raise ValueError("separators must not contain duplicates")


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


@dataclass(frozen=True, slots=True)
class ChunkHeaderContext:
    """The facts a contextual header may describe for one evidence chunk.

    Every field is optional; a contextualizer renders only what is present.
    ``part_index`` is one-based and travels with ``part_count``.
    """

    document_title: str | None = None
    structural_path: tuple[str, ...] = ()
    caption: str | None = None
    code_location: str | None = None
    language: str | None = None
    scope: tuple[str, ...] = ()
    part_index: int | None = None
    part_count: int | None = None

    def __post_init__(self) -> None:
        if any(not part.strip() for part in self.structural_path):
            raise ValueError("header structural_path parts must be non-empty")
        if any(not line.strip() for line in self.scope):
            raise ValueError("header scope lines must be non-empty")
        if (self.part_index is None) != (self.part_count is None):
            raise ValueError("part_index and part_count must be provided together")
        if self.part_index is not None and self.part_count is not None:
            if not 1 <= self.part_index <= self.part_count:
                raise ValueError("part_index must satisfy 1 <= part_index <= part_count")


@runtime_checkable
class ChunkContextualizer(Protocol):
    """Render a deterministic context header in front of chunk content.

    ``format_version`` identifies the header format; changing the format must
    change it so embeddings are rebuilt deliberately rather than silently mixed.
    """

    format_version: str

    def header(self, context: ChunkHeaderContext, *, maximum_tokens: int | None = None) -> str:
        """Return the header, trimmed to ``maximum_tokens`` when a budget is given."""

    def render(self, header: str, content: str) -> str:
        """Join a header and chunk content into the text that gets embedded."""
