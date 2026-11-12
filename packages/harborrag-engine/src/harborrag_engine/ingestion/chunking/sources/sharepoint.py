from __future__ import annotations

from dataclasses import replace
from typing import Any

from harborrag_core.contracts.chunking import TokenCounter

from ..config import ChunkingProfile
from ..schemas import ChunkingRequest, ChunkUnit
from ..transforms import DocumentStructureSegmenter

# SharePoint identity and provenance copied onto every chunk of a document. The
# payload keys are the metadata schema: a point carries these names directly.
_EXTRA_FIELDS = ("site_id", "drive_id", "item_id", "page_id")


class SharePointChunkingStrategy:
    """Enrich canonical SharePoint units with source identity and provenance.

    Every unit of one document carries the same values, so the packer keeps them
    at the chunk's top level and each resulting Qdrant point retains them.
    """

    name = "sharepoint"
    version = "1"

    def __init__(self, token_counter: TokenCounter) -> None:
        self._segmenter = DocumentStructureSegmenter(token_counter)

    def create_units(
        self,
        request: ChunkingRequest,
        profile: ChunkingProfile,
    ) -> tuple[ChunkUnit, ...]:
        """Return canonical units, each carrying the document's SharePoint metadata."""

        provenance = self._document_metadata(request)
        return tuple(
            replace(unit, metadata={**provenance, **unit.metadata})
            for unit in self._segmenter.segment(request.document, profile)
        )

    @staticmethod
    def _document_metadata(request: ChunkingRequest) -> dict[str, Any]:
        document = request.document
        extra = document.provenance.extra
        values: dict[str, Any] = {
            key: str(extra[key]).strip()
            for key in _EXTRA_FIELDS
            if extra.get(key) is not None and str(extra[key]).strip()
        }
        web_url = document.provenance.url or extra.get("url")
        if web_url is not None and str(web_url).strip():
            values["web_url"] = str(web_url).strip()
        if document.provenance.author:
            values["author"] = document.provenance.author
        updated = extra.get("source_updated_at")
        if updated is None and document.provenance.updated_at is not None:
            updated = document.provenance.updated_at.isoformat()
        if updated is not None:
            values["source_updated_at"] = str(updated)
        return values
