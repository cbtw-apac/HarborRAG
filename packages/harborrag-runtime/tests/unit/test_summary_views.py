"""Summaries are authorized read-time projections, never canonical Chunk content."""

from unittest.mock import AsyncMock

import pytest

from harborrag_core.ingestion import GraphNodeRecord
from harborrag_core.retrieval.graph import compact_node
from harborrag_core.security.context import AccessContext
from harborrag_core.summary_cards import SummaryCard, SummaryView
from harborrag_runtime.retrieval.summary_views import apply_summary_views


def record(kind):
    return GraphNodeRecord(
        node_key=kind,
        node_kind=kind,
        logical_id=kind,
        owner_id="DEFAULT",
        ownership_scope="DOCUMENT_VERSION" if kind == "Chunk" else "SOURCE_SCOPE",
        source_scope_id="scope",
        document_id="doc" if kind == "Chunk" else None,
        document_version_id="version" if kind == "Chunk" else None,
        description="Original description.",
    )


@pytest.mark.asyncio
async def test_read_join_preserves_chunks_and_does_not_persist_summary():
    nodes = tuple(record(kind) for kind in ("DataSource", "Chunk"))
    view = SummaryView(status="current", card=SummaryCard(description="Accepted source card."))
    reader = AsyncMock()
    reader.views.return_value = {"DataSource": view}
    result = await apply_summary_views(nodes, reader, AccessContext.system("DEFAULT"))
    assert reader.views.call_args.args[1] == ("DataSource",)
    assert result[1:] == nodes[1:]
    assert result[0].description == "Accepted source card."
    assert "summary" not in result[0].model_dump()
    assert compact_node(result[0])["summary"]["card"]["description"] == "Accepted source card."


@pytest.mark.asyncio
async def test_denied_or_pending_card_replaces_legacy_content_with_safe_metadata():
    node = record("DataSource")
    reader = AsyncMock()
    reader.views.return_value = {node.node_key: SummaryView()}
    result = await apply_summary_views((node,), reader, AccessContext.system("DEFAULT"))
    assert result[0].description != node.description
    assert result[0].summary.card is None


def test_summary_cards_reject_blank_or_oversized_presentation_values():
    with pytest.raises(ValueError, match="1 to 60 words"):
        SummaryCard(description=" ")
    with pytest.raises(ValueError, match="1 to 60 words"):
        SummaryCard(description="word " * 61)
    with pytest.raises(ValueError, match="facets"):
        SummaryCard(description="Valid.", topics=(" ",))
    with pytest.raises(ValueError, match="facets"):
        SummaryCard(description="Valid.", key_entities=("x" * 81,))
