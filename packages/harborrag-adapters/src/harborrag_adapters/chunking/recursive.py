from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

from harborrag_core.contracts.chunking import (
    SplitBoundaryKind,
    TextRefinementRequest,
    TextSplit,
    TokenCounter,
)
from harborrag_core.ingestion import ChunkValidationError

from ._spans import source_span, validate_coverage

_PROSE_SEPARATORS = ("\n\n", "\n", ". ", "? ", "! ", "。", "？", "！", " ", "")
_STRUCTURED_SEPARATORS = ("\n", "")


class RecursiveTextRefiner:
    """Use recursive character splitting only to refine oversized text units."""

    name = "recursive"
    version = "2"

    def __init__(
        self,
        token_counter: TokenCounter,
        *,
        splitter_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._token_counter = token_counter
        self._splitter_factory = splitter_factory

    def split(self, request: TextRefinementRequest) -> tuple[TextSplit, ...]:
        """Split text at a hard maximum while retaining ordered source spans."""

        if not request.content.strip():
            return ()

        original_count = self._token_counter.count(request.content)
        if original_count < 1:
            return ()
        if original_count <= request.maximum_tokens:
            return (
                TextSplit(
                    content=request.content,
                    token_count=original_count,
                    source_span=request.source_span,
                    boundary_kind=request.boundary_kind,
                    structural_path=request.structural_path,
                ),
            )

        splitter_factory = self._splitter_factory or self._load_splitter_factory()
        separators = request.separators or (
            _STRUCTURED_SEPARATORS
            if request.preserve_whitespace
            and request.boundary_kind
            in {
                SplitBoundaryKind.CODE_BLOCK,
                SplitBoundaryKind.CODE_SYMBOL,
                SplitBoundaryKind.LINE,
                SplitBoundaryKind.TABLE,
                SplitBoundaryKind.TABLE_ROW,
            }
            else _PROSE_SEPARATORS
        )
        splitter = splitter_factory(
            chunk_size=request.maximum_tokens,
            chunk_overlap=request.overlap_tokens,
            length_function=self._token_counter.count,
            separators=list(separators),
            keep_separator=True,
            strip_whitespace=False,
            add_start_index=True,
        )

        positioned = self._positioned_chunks(splitter, request.content)
        if not positioned:
            raise ChunkValidationError("recursive text refiner returned no content")
        validate_coverage([(start, end) for _, start, end in positioned], len(request.content))

        forced = len(positioned) > 1
        results: list[TextSplit] = []
        for content, start, end in positioned:
            token_count = self._token_counter.count(content)
            if token_count < 1:
                raise ChunkValidationError("recursive text refiner returned a zero-token split")
            if token_count > request.maximum_tokens:
                raise ChunkValidationError(
                    "recursive text refiner returned content above maximum_tokens; "
                    "use a tokenizer-compatible length function"
                )
            results.append(
                TextSplit(
                    content=content,
                    token_count=token_count,
                    source_span=source_span(request, start, end),
                    boundary_kind=(SplitBoundaryKind.FORCED if forced else request.boundary_kind),
                    structural_path=request.structural_path,
                    forced_split=forced,
                )
            )
        return tuple(results)

    @staticmethod
    def _load_splitter_factory() -> Callable[..., Any]:
        try:
            from langchain_text_splitters import RecursiveCharacterTextSplitter
        except ImportError as exc:
            raise ImportError(
                "Recursive text refinement requires the harborrag-adapters[chunking] extra"
            ) from exc
        return cast(Callable[..., Any], RecursiveCharacterTextSplitter)

    @staticmethod
    def _positioned_chunks(splitter: Any, text: str) -> list[tuple[str, int, int]]:
        if hasattr(splitter, "create_documents"):
            documents = splitter.create_documents([text])
            positioned: list[tuple[str, int, int]] = []
            for document in documents:
                content = str(document.page_content)
                if not content:
                    continue
                metadata: Mapping[str, Any] = document.metadata
                start = metadata.get("start_index")
                if (
                    not isinstance(start, int)
                    or start < 0
                    or start + len(content) > len(text)
                    or text[start : start + len(content)] != content
                ):
                    positioned = []
                    break
                positioned.append((content, start, start + len(content)))
            if positioned:
                return positioned

        values: Sequence[str] = splitter.split_text(text)
        positioned = []
        previous_start = 0
        previous_end = 0
        for value in values:
            if not value:
                continue
            start = text.find(value, previous_start)
            if start < 0 and previous_end:
                start = text.find(value, 0, previous_end)
            if start < 0:
                raise ChunkValidationError("could not map recursive split back to source content")
            end = start + len(value)
            positioned.append((value, start, end))
            previous_start = start + 1
            previous_end = end
        return positioned

    def fingerprint(self) -> str:
        """Identify the boundary behaviour for processing-identity hashes."""

        return f"{self.name}:{self.version}"
