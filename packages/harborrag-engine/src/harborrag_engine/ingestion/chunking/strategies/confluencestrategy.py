from __future__ import annotations

import json
from dataclasses import replace

from harborrag_core.contracts.chunking import TokenCounter

from ..config import ChunkingProfile
from ..schemas import ChunkingRequest, ChunkUnit
from ..segmentation import DocumentStructureSegmenter


class ConfluenceChunkingStrategy:
    """Create page- and section-aware units for Confluence content."""

    name = "confluence"
    version = "2"

    def __init__(self, token_counter: TokenCounter) -> None:
        self._segmenter = DocumentStructureSegmenter(token_counter)

    def create_units(
        self,
        request: ChunkingRequest,
        profile: ChunkingProfile,
    ) -> tuple[ChunkUnit, ...]:
        """Enrich structural document units with Confluence provenance."""

        provenance = dict(request.document.provenance.extra)
        page_id = (
            provenance.get("page_id")
            or provenance.get("content_id")
            or request.document.provenance.record_id
        )
        units = self._segmenter.segment(request.document, profile)
        output: list[ChunkUnit] = []
        for unit in units:
            path = json.dumps(
                unit.structural_path,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            metadata = {
                **provenance,
                **unit.metadata,
                "page_id": page_id,
                "page_title": request.document.title,
                "heading_path": unit.structural_path,
            }
            unique_role = unit.role in {"table", "code", "figure", "caption"}
            output.append(
                replace(
                    unit,
                    anchor=f"page:{page_id}/heading:{path}/{unit.anchor}",
                    merge_group=(
                        f"page:{page_id}:{unit.role}:{unit.anchor}"
                        if unique_role
                        else f"page:{page_id}:heading:{path}:{unit.role}"
                    ),
                    hard_boundary_before=unique_role,
                    hard_boundary_after=unique_role,
                    metadata=metadata,
                )
            )
        return tuple(output)
