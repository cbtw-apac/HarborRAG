from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from harborrag_core.contracts.chunking import (
    SplitBoundaryKind,
    StructureSplitRequest,
    TextSplit,
    TokenCounter,
)

from .base import HarborBaseChunk
from .provenance import metadata_path, structural_span

_HEADERS = tuple(("#" * level, f"h{level}") for level in range(1, 7))
_HEADER_KEYS = tuple(value for _, value in _HEADERS)


class MarkdownStructureSplitter(HarborBaseChunk[StructureSplitRequest]):
    """Recover Markdown heading paths as HarborRAG structural splits."""

    chunk_name = "markdown"
    required_dependency = "langchain_text_splitters"
    request_type = StructureSplitRequest

    def __init__(
        self,
        token_counter: TokenCounter,
        *,
        strip_headers: bool = False,
        splitter_factory: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__(token_counter)
        self._strip_headers = strip_headers
        self._splitter_factory = splitter_factory

    def split(self, request: StructureSplitRequest) -> tuple[TextSplit, ...]:
        """Convert Markdown sections into ordered HarborRAG split results."""

        if not request.content.strip():
            return ()
        factory = self._splitter_factory or self._load_splitter_factory()
        splitter = factory(
            headers_to_split_on=list(_HEADERS),
            strip_headers=self._strip_headers,
        )
        results: list[TextSplit] = []
        for document in splitter.split_text(request.content):
            content = str(document.page_content)
            if not content.strip():
                continue
            count = self._token_counter.count(content)
            if count < 1:
                continue
            results.append(
                TextSplit(
                    content=content,
                    token_count=count,
                    source_span=structural_span(request.source_span),
                    boundary_kind=SplitBoundaryKind.SECTION,
                    structural_path=metadata_path(document.metadata, _HEADER_KEYS),
                )
            )
        return tuple(results)

    @staticmethod
    def _load_splitter_factory() -> Callable[..., Any]:
        try:
            from langchain_text_splitters import MarkdownHeaderTextSplitter
        except ImportError as exc:
            raise ImportError(
                "Markdown structure splitting requires the harborrag-adapters[chunking] extra"
            ) from exc
        return cast(Callable[..., Any], MarkdownHeaderTextSplitter)
