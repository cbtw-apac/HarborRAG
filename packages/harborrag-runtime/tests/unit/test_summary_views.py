"""Summaries are authorized read-time projections, never canonical Chunk content."""

from unittest.mock import AsyncMock

import pytest

from harborrag_core.ingestion import GraphNodeRecord
from harborrag_core.retrieval.graph import compact_node
from harborrag_core.security.context import AccessContext
from harborrag_core.summaries import CardWordBudgets, SummaryPolicy
from harborrag_core.summary_cards import SummaryAttribute, SummaryCard, SummaryView
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
    with pytest.raises(ValueError, match="1 to 512 words"):
        SummaryCard(description=" ")
    with pytest.raises(ValueError, match="1 to 512 words"):
        SummaryCard(description="word " * 513)
    with pytest.raises(ValueError, match="facets"):
        SummaryCard(description="Valid.", topics=(" ",))
    with pytest.raises(ValueError, match="facets"):
        SummaryCard(description="Valid.", key_entities=("x" * 81,))


def test_the_card_ceiling_is_wide_but_the_enforced_budget_is_the_policy_s():
    """A dossier fits the type; what a given kind may spend is the policy's call."""

    dossier = SummaryCard(description="word " * 200)
    assert len(dossier.description.split()) == 200
    budgets = SummaryPolicy(model_fingerprint="model").card_words
    assert budgets.for_kind("Structure") == 60
    assert budgets.for_kind("SourceEntity") == 60
    widened = SummaryPolicy(
        model_fingerprint="model", card_words=CardWordBudgets(source_entity=400)
    )
    assert widened.card_words.for_kind("SourceEntity") == 400
    assert widened.card_words.for_kind("DocumentVersion") == 60
    # A budget is model-affecting, so it has to move the fingerprint.
    assert widened.fingerprint != SummaryPolicy(model_fingerprint="model").fingerprint


def test_attributes_are_distinct_named_facets_with_their_own_provenance():
    card = SummaryCard(
        description="Valid.",
        attributes=(
            SummaryAttribute(name="stage", values=("round 2",), from_document_ids=("doc-1",)),
            SummaryAttribute(name="skills", values=("java", "kubernetes")),
        ),
    )
    assert card.attributes[0].from_document_ids == ("doc-1",)
    assert card.attributes[1].from_document_ids == ()
    with pytest.raises(ValueError, match="distinct"):
        SummaryCard(
            description="Valid.",
            attributes=(
                SummaryAttribute(name="stage", values=("a",)),
                SummaryAttribute(name="stage", values=("b",)),
            ),
        )
    with pytest.raises(ValueError, match="distinct"):
        SummaryAttribute(name="skills", values=("java", "java"))
