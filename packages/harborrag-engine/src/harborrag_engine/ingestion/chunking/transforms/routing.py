from __future__ import annotations

import json
from collections.abc import Sequence

from harborrag_core.contracts.chunking import SplitBoundaryKind, TokenCounter
from harborrag_core.ingestion import UnsupportedDocumentError

from ..config import ROUTE_MAXIMUM_TOKENS
from ..schemas import ChunkCandidate, ChunkingRequest, ChunkUnit
from .segmentation import element_span

_ROUTE_METADATA_FIELDS = (
    "space_id",
    "space_key",
    "project_id",
    "project_key",
    "issue_key",
    "filename",
    "relative_path",
)

# Each free-form field is bounded before assembly so that no single one can
# crowd the others out of the route budget.
_FIELD_CHARACTERS = 320


class RouteChunkPlanner:
    """Create compact document and section routes alongside source evidence."""

    def __init__(self, token_counter: TokenCounter) -> None:
        self._token_counter = token_counter

    def prepend(
        self,
        request: ChunkingRequest,
        evidence: tuple[ChunkCandidate, ...],
        *,
        enabled: bool,
    ) -> tuple[ChunkCandidate, ...]:
        if not enabled:
            return evidence
        if not evidence:
            return (self._document_only_route(request),)
        first = evidence[0]
        content = self._content(request, evidence)
        metadata = {
            **first.metadata,
            "route_level": "document",
        }
        document_route = ChunkCandidate(
            anchor="route:document",
            content=content,
            token_count=self._token_counter.count(content),
            role="route",
            structural_path=(),
            source_span=first.source_span,
            units=first.units,
            boundary_kind=SplitBoundaryKind.DOCUMENT,
            metadata=metadata,
        )
        routed: list[ChunkCandidate] = [document_route]
        seen_sections: set[tuple[str, ...]] = set()
        for candidate in evidence:
            section_path = candidate.structural_path
            if section_path and section_path not in seen_sections:
                routed.append(self._section_route(request, candidate))
                seen_sections.add(section_path)
            routed.append(candidate)
        return tuple(routed)

    def _document_only_route(self, request: ChunkingRequest) -> ChunkCandidate:
        """Build a route for a navigational document that has no body evidence.

        Confluence and similar sources legitimately contain title/heading-only
        pages.  Their route remains useful for navigation, but it must retain
        real source provenance instead of inventing an evidence chunk.
        """

        element = next(
            (
                item
                for item in request.document.content
                if item.content is not None and item.content.strip()
            ),
            None,
        )
        if element is None:
            raise UnsupportedDocumentError(
                "normalized document contains no indexable source content"
            )
        source_content = element.content or ""
        source_span = element_span(element.id, source_content, element.metadata)
        source_unit = ChunkUnit(
            anchor=f"route-source:{element.id}",
            content=source_content,
            token_count=self._token_counter.count(source_content),
            role="route.source",
            structural_path=(),
            source_span=source_span,
            merge_group=f"route-source:{element.id}",
            boundary_kind=SplitBoundaryKind.DOCUMENT,
            metadata={
                **request.document.provenance.extra,
                **element.metadata,
            },
        )
        content = self._content(request, ())
        return ChunkCandidate(
            anchor="route:document",
            content=content,
            token_count=self._token_counter.count(content),
            role="route",
            structural_path=(),
            source_span=source_span,
            units=(source_unit,),
            boundary_kind=SplitBoundaryKind.DOCUMENT,
            metadata={**source_unit.metadata, "route_level": "document"},
        )

    def _section_route(
        self,
        request: ChunkingRequest,
        evidence: ChunkCandidate,
    ) -> ChunkCandidate:
        path = evidence.structural_path
        content = self._fit(
            "\n".join(
                (
                    f"Document: {request.document.title.strip()[:_FIELD_CHARACTERS]}",
                    f"Section: {' > '.join(path)[:_FIELD_CHARACTERS]}",
                    f"Extract: {' '.join(evidence.content.split())[:_FIELD_CHARACTERS]}",
                )
            )
        )
        return ChunkCandidate(
            anchor=f"route:section:{json.dumps(path, ensure_ascii=False, separators=(',', ':'))}",
            content=content,
            token_count=self._token_counter.count(content),
            role="section.route",
            structural_path=path,
            source_span=evidence.source_span,
            units=evidence.units,
            boundary_kind=SplitBoundaryKind.SECTION,
            metadata={**evidence.metadata, "route_level": "section"},
        )

    def _fit(self, content: str) -> str:
        """Trim assembled route content to the route budget.

        Routes are validated against a hard ceiling, and a route that crosses it
        fails the whole document rather than degrading. Bounding each field is
        not enough on its own -- a long title plus many metadata fields can
        still cross it -- so the assembled text gets a final deterministic trim.
        """

        if self._token_counter.count(content) <= ROUTE_MAXIMUM_TOKENS:
            return content
        low, high = 1, len(content)
        while low < high:
            middle = (low + high + 1) // 2
            if self._token_counter.count(content[:middle]) <= ROUTE_MAXIMUM_TOKENS:
                low = middle
            else:
                high = middle - 1
        return content[:low].rstrip()

    def _content(
        self,
        request: ChunkingRequest,
        evidence: tuple[ChunkCandidate, ...],
    ) -> str:
        document = request.document
        values: list[str] = [f"Title: {document.title.strip()[:_FIELD_CHARACTERS]}"]
        record_id = document.provenance.record_id
        if record_id:
            values.append(f"Source ID: {record_id[:_FIELD_CHARACTERS]}")
        for field in _ROUTE_METADATA_FIELDS:
            value = document.provenance.extra.get(field)
            rendered = _render_value(value)
            if rendered:
                values.append(f"{field.replace('_', ' ').title()}: {rendered[:_FIELD_CHARACTERS]}")
        labels = tuple(
            dict.fromkeys(
                (
                    *document.provenance.tags,
                    *_string_values(document.provenance.extra.get("labels")),
                )
            )
        )
        if labels:
            values.append(f"Labels: {', '.join(labels)[:_FIELD_CHARACTERS]}")
        headings = tuple(
            dict.fromkeys(
                candidate.structural_path[0] for candidate in evidence if candidate.structural_path
            )
        )
        if headings:
            values.append(f"Major headings: {', '.join(headings)[:_FIELD_CHARACTERS]}")
        if evidence:
            extract = " ".join(evidence[0].content.split())
            if extract:
                values.append(f"Extract: {extract[:_FIELD_CHARACTERS]}")
        return self._fit("\n".join(values))


def _string_values(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(
            str(item).strip()
            for item in value
            if isinstance(item, (str, int)) and str(item).strip()
        )
    return ()


def _render_value(value: object) -> str:
    if isinstance(value, (str, int)):
        return str(value).strip()
    return ", ".join(_string_values(value))
