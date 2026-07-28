from __future__ import annotations

from collections.abc import Mapping, Sequence

from harborrag_core.contracts.chunking import (
    JsonStructureSplitRequest,
    JsonStructureSplitter,
    SourceSpan,
    SplitBoundaryKind,
    TokenCounter,
)

from ..config import ChunkingProfile
from ..schemas import ChunkingRequest, ChunkUnit
from ..segmentation import DocumentStructureSegmenter


class JsonChunkingStrategy:
    """Create structural units along JSON object and array paths."""

    name = "json"
    version = "2"

    def __init__(
        self,
        token_counter: TokenCounter,
        splitter: JsonStructureSplitter | None,
    ) -> None:
        self._splitter = splitter
        self._segmenter = DocumentStructureSegmenter(token_counter)

    def create_units(
        self,
        request: ChunkingRequest,
        profile: ChunkingProfile,
    ) -> tuple[ChunkUnit, ...]:
        """Split raw JSON when available or segment normalized elements."""

        raw_value = self._raw_json(request)
        if self._splitter is None or raw_value is None:
            return self._segmenter.segment(request.document, profile)

        element_ids = tuple(element.id for element in request.document.content)
        splits = self._splitter.split(
            JsonStructureSplitRequest(
                value=raw_value,
                maximum_characters=max(profile.maximum_tokens * 4, 1),
                source_span=SourceSpan(element_ids=element_ids),
            )
        )
        if not splits:
            return self._segmenter.segment(request.document, profile)
        units: list[ChunkUnit] = []
        for split in splits:
            json_path = "$" + "".join(f"[{part!r}]" for part in split.structural_path)
            units.append(
                ChunkUnit(
                    anchor=f"json:{json_path}",
                    content=split.content,
                    token_count=split.token_count,
                    role="json",
                    structural_path=split.structural_path,
                    source_span=split.source_span or SourceSpan(element_ids=element_ids),
                    merge_group=f"json:{json_path}",
                    boundary_kind=SplitBoundaryKind.JSON_PATH,
                    hard_boundary_before=True,
                    hard_boundary_after=True,
                    metadata={
                        "json_path": json_path,
                        "record_key": (
                            split.structural_path[-1] if split.structural_path else None
                        ),
                    },
                )
            )
        return tuple(units)

    @staticmethod
    def _raw_json(
        request: ChunkingRequest,
    ) -> Mapping[str, object] | Sequence[object] | None:
        raw = request.document.raw
        if not isinstance(raw, Mapping):
            return None
        value = raw.get("json")
        if isinstance(value, Mapping):
            return value
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return value
        return None
