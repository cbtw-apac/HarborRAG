from __future__ import annotations

from harborrag_core.contracts.chunking import TokenCounter

from ..config import ChunkingProfile
from ..schemas import ChunkingRequest, ChunkUnit
from ..transforms import DocumentStructureSegmenter


class CanonicalDocumentChunkingStrategy:
    """Build structural units from source-neutral canonical elements.

    Parsing and normalization own raw Markdown, HTML, JSON, and attachment
    formats. This fallback intentionally depends only on the canonical
    document contract so adding a connector does not expand this module.
    """

    name = "canonical"
    version = "1"

    def __init__(self, token_counter: TokenCounter) -> None:
        self._segmenter = DocumentStructureSegmenter(token_counter)

    def create_units(
        self,
        request: ChunkingRequest,
        profile: ChunkingProfile,
    ) -> tuple[ChunkUnit, ...]:
        """Create units from normalized source elements."""

        return self._segmenter.segment(request.document, profile)
