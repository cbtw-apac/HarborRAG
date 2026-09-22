"""One section must get one id, whichever side of chunking derives it.

A chunk's own ``section_id`` is assigned during identity assignment; the
ancestry a deeper chunk carries is built by the record context builder. Graph
projection keys SECTION nodes on ``(path, logical_id)`` and reads the first
from a terminal chunk and the second from a deeper one, so the two derivations
have to produce the same id for the same section. They were computed by two
copies of one helper, and only one of them required an anchor per path segment
-- so for a connector path with a label that carries no heading ID (a
Confluence tab), the same section arrived under two ids and the graph grew two
nodes for it, splitting the subtree.
"""

from __future__ import annotations

import pytest

from harborrag_core.chunking import ChunkKind
from harborrag_core.contracts.chunking import SourceSpan
from harborrag_engine.ingestion.chunking.anchors import section_anchors
from harborrag_engine.ingestion.chunking.identity import ChunkIdentityBuilder, ChunkIdentityInput
from harborrag_engine.ingestion.chunking.records import ChunkContextBuilder
from harborrag_engine.ingestion.chunking.schemas import (
    ChunkCandidate,
    ChunkUnit,
    SplitBoundaryKind,
)

from .chunking_helpers import make_document, make_request

pytestmark = pytest.mark.unit

_SPAN = SourceSpan(start_offset=0, end_offset=10)
# A tab label with no heading element ID of its own, then two real headings:
# one anchor short of the path, which is what the two helpers disagreed about.
_TAB_ANCHORS = ("heading-1", "heading-2")


def _candidate(path: tuple[str, ...], anchors: tuple[str, ...]) -> ChunkCandidate:
    unit = ChunkUnit(
        anchor=f"anchor-{len(path)}",
        content="body text",
        token_count=2,
        role="content",
        structural_path=path,
        source_span=_SPAN,
        merge_group="group-1",
    )
    return ChunkCandidate(
        anchor=unit.anchor,
        content=unit.content,
        token_count=unit.token_count,
        role=unit.role,
        structural_path=path,
        source_span=_SPAN,
        units=(unit,),
        boundary_kind=SplitBoundaryKind.PARAGRAPH,
        metadata={"heading_element_ids": anchors},
    )


def _identity(builder: ChunkIdentityBuilder, candidate: ChunkCandidate) -> str:
    return builder.identify(
        ChunkIdentityInput(
            document_id="doc-1",
            document_version_id="document-version:1",
            strategy_version="1",
            section_path=candidate.structural_path,
            structural_anchor=candidate.anchor,
            local_part_index=candidate.local_part_index,
            chunk_kind=ChunkKind.TEXT,
            content_hash="hash-1",
            section_anchors=section_anchors(candidate),
        )
    ).section_id


def test_a_partly_anchored_path_yields_no_anchors_at_all() -> None:
    """Anchors are positional, so a list that misses a segment cannot be sliced."""

    partial = _candidate(("Tab", "Heading", "Sub"), _TAB_ANCHORS)
    complete = _candidate(("Heading", "Sub"), _TAB_ANCHORS)

    assert section_anchors(partial) == ()
    assert section_anchors(complete) == _TAB_ANCHORS


def test_a_chunks_section_id_equals_the_ancestry_entry_a_deeper_chunk_carries() -> None:
    """The equality graph projection depends on to emit one node per section."""

    builder = ChunkIdentityBuilder()
    request = make_request(make_document([]))
    section_path = ("Tab", "Heading")
    terminal = _candidate(section_path, _TAB_ANCHORS[:1])
    deeper = _candidate((*section_path, "Sub"), _TAB_ANCHORS)

    section_id = _identity(builder, terminal)
    hierarchy = ChunkContextBuilder(builder).hierarchy(
        request,
        deeper,
        builder.identify(
            ChunkIdentityInput(
                document_id="doc-1",
                document_version_id="document-version:1",
                strategy_version="1",
                section_path=deeper.structural_path,
                structural_anchor=deeper.anchor,
                local_part_index=0,
                chunk_kind=ChunkKind.TEXT,
                content_hash="hash-2",
                section_anchors=section_anchors(deeper),
            )
        ),
        None,
        None,
    )

    assert hierarchy.ancestry[len(section_path) - 1] == section_id
    assert hierarchy.parent_section_id == section_id
