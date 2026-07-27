from __future__ import annotations

import json
from collections.abc import Mapping

from harborrag_core.contracts.chunking import (
    SourceSpan,
    StructureSplitRequest,
    StructureSplitter,
    TokenCounter,
)

from ..config import ChunkingProfile
from ..schemas import ChunkingRequest, ChunkUnit
from ..segmentation import DocumentStructureSegmenter


class DocumentChunkingStrategy:
    """Create structural units from parser elements, with raw-format fallback."""

    name = "document"
    version = "2"

    def __init__(
        self,
        token_counter: TokenCounter,
        *,
        markdown_splitter: StructureSplitter | None = None,
        html_splitter: StructureSplitter | None = None,
    ) -> None:
        self._token_counter = token_counter
        self._segmenter = DocumentStructureSegmenter(token_counter)
        self._markdown_splitter = markdown_splitter
        self._html_splitter = html_splitter

    def create_units(
        self,
        request: ChunkingRequest,
        profile: ChunkingProfile,
    ) -> tuple[ChunkUnit, ...]:
        """Create hierarchy-aware units, using a raw-format fallback if needed."""

        fallback = self._fallback_splitter(request)
        raw_text = self._raw_text(request)
        has_heading_structure = any(
            element.type == "heading" for element in request.document.content
        )
        if fallback is not None and raw_text is not None and not has_heading_structure:
            fallback_units = self._fallback_units(request, fallback, raw_text)
            if fallback_units:
                return fallback_units
        return self._segmenter.segment(request.document, profile)

    def _fallback_splitter(self, request: ChunkingRequest) -> StructureSplitter | None:
        if request.content_type in {"text/markdown", "text/x-markdown"}:
            return self._markdown_splitter
        if request.content_type in {"text/html", "application/xhtml+xml"}:
            return self._html_splitter
        return None

    @staticmethod
    def _raw_text(request: ChunkingRequest) -> str | None:
        raw = request.document.raw
        if not isinstance(raw, Mapping):
            return None
        format_key = (
            "markdown" if request.content_type in {"text/markdown", "text/x-markdown"} else "html"
        )
        for key in (format_key, "source_text", "content"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return None

    def _fallback_units(
        self,
        request: ChunkingRequest,
        splitter: StructureSplitter,
        raw_text: str,
    ) -> tuple[ChunkUnit, ...]:
        element_ids = tuple(element.id for element in request.document.content)
        splits = splitter.split(
            StructureSplitRequest(
                content=raw_text,
                source_span=SourceSpan(element_ids=element_ids),
            )
        )
        units: list[ChunkUnit] = []
        for index, split in enumerate(splits):
            path = json.dumps(
                split.structural_path,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            units.append(
                ChunkUnit(
                    anchor=f"fallback-section:{path}:part:{index}",
                    content=split.content,
                    token_count=split.token_count,
                    role="text",
                    structural_path=split.structural_path,
                    source_span=split.source_span or SourceSpan(element_ids=element_ids),
                    merge_group=f"fallback-section:{path}",
                    boundary_kind=split.boundary_kind,
                    forced_split=split.forced_split,
                    metadata={"format_fallback": request.content_type},
                )
            )
        return tuple(units)
