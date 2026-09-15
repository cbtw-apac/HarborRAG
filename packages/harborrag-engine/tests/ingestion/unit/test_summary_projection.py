"""Semantic reductions reuse content and assign one owner to every contribution."""

import pytest

from harborrag_core.domain.element import DocumentElement
from harborrag_core.ingestion import KnowledgeNodeKind
from harborrag_core.summaries import SummaryCard, SummaryPolicy
from harborrag_core.topology.derived import DescriptionOutput
from harborrag_engine.ingestion import GraphProjectionBuilder, GraphProjectionInput
from harborrag_engine.topology.summary_planner import summary_plan
from harborrag_engine.topology.summary_reducer import SummaryBudgetDeferred, SummaryReducer

from .chunking_helpers import make_document, make_profile, make_request, make_service


class Cache:
    def __init__(self):
        self.cards = {}

    async def get_card(self, tenant_id, key):
        return self.cards.get((tenant_id, key))

    async def put_card(self, tenant_id, key, card):
        return self.cards.setdefault((tenant_id, key), card)


class Generator:
    def __init__(self):
        self.calls = []

    async def generate(self, packets):
        self.calls.append(packets)
        return DescriptionOutput(
            description="Published deployment instructions.",
            topics=("deployment",),
            cited_packet_ids=(packets[0].packet_id,),
            complete=True,
        )


@pytest.mark.asyncio
async def test_reuses_exact_inputs_and_preserves_facets_but_isolates_tenants_and_policies():
    cache, generator = Cache(), Generator()
    policy = SummaryPolicy(model_fingerprint="deployment-v1")
    first = await SummaryReducer("one", policy, cache, generator).reduce(
        "Structure", {"name": "Deploy"}, ("Deploy after approval.",)
    )
    second = await SummaryReducer("one", policy, cache, generator).reduce(
        "Structure", {"name": "Deploy"}, ("Deploy after approval.",)
    )
    assert first == second
    assert len(generator.calls) == 1
    assert first[0].topics == ("deployment",)
    await SummaryReducer("two", policy, cache, generator).reduce(
        "Structure", {"name": "Deploy"}, ("Deploy after approval.",)
    )
    await SummaryReducer(
        "one", policy.model_copy(update={"model_fingerprint": "deployment-v2"}), cache, generator
    ).reduce("Structure", {"name": "Deploy"}, ("Deploy after approval.",))
    assert len(generator.calls) == 3
    assert all(packet.packet_id.startswith("p") for call in generator.calls for packet in call)


@pytest.mark.asyncio
async def test_singleton_is_bounded_empty_is_deterministic_and_large_text_is_not_truncated():
    cache, generator = Cache(), Generator()
    policy = SummaryPolicy(model_fingerprint="model", max_input_bytes=2048, max_input_tokens=512)
    reducer = SummaryReducer("tenant", policy, cache, generator)
    empty, _ = await reducer.reduce("SourceEntity", {}, ())
    assert not generator.calls
    assert "No published content" in empty.description
    source = "Beginning " + "bounded input " * 900 + " THE_END"
    card, _ = await reducer.reduce("Structure", {}, (source,))
    assert len(card.description) <= 480
    all_inputs = [packet.text for call in generator.calls for packet in call]
    assert any("Beginning" in value for value in all_inputs)
    assert any("THE_END" in value for value in all_inputs)
    assert all(len(packet.text.encode()) <= 2048 for call in generator.calls for packet in call)


@pytest.mark.asyncio
async def test_budget_deferral_keeps_completed_reductions_reusable():
    cache, generator = Cache(), Generator()
    policy = SummaryPolicy(model_fingerprint="model", max_calls=1, max_fan_in=2)
    inputs = tuple(f"Unique content {index}." for index in range(9))
    with pytest.raises(SummaryBudgetDeferred):
        await SummaryReducer("tenant", policy, cache, generator).reduce("DataSource", {}, inputs)
    assert len(generator.calls) == 1
    assert cache.cards
    await SummaryReducer(
        "tenant", policy.model_copy(update={"max_calls": 64}), cache, generator
    ).reduce("DataSource", {}, inputs)
    assert sum(call == generator.calls[0] for call in generator.calls) == 1


def test_nested_section_and_table_have_one_owner_and_complete_document_coverage():
    document = make_document(
        [
            DocumentElement("h1", "heading", "Operations", {"level": 1}),
            DocumentElement("p1", "paragraph", "Run the worker."),
            DocumentElement("h2", "heading", "Options", {"level": 2}),
            DocumentElement("t1", "table", "Mode\tTimeout\nprod\t30\n"),
        ]
    )
    chunks = make_service(make_profile(target=40, maximum=60)).chunk(make_request(document)).chunks
    graph = GraphProjectionBuilder().build(
        GraphProjectionInput(
            document=document,
            chunks=chunks,
            resolved_targets={},
            graph_projection_version="graph-v1",
        )
    )
    planned = summary_plan(graph.nodes, graph.relations, chunks)
    direct = [str(chunk.chunk_id) for item in planned for chunk in item.direct_chunks]
    assert len(direct) == len(set(direct)) == len(chunks)
    version = next(item for item in planned if item.kind == "DocumentVersion")
    assert set(version.input_chunk_ids) == {str(chunk.chunk_id) for chunk in chunks}
    table = next(item for item in planned if item.node.entity_type.value == "table")
    assert table.input_chunk_ids
    assert all(item.node.node_kind != KnowledgeNodeKind.CHUNK for item in planned)
    assert (
        summary_plan(tuple(reversed(graph.nodes)), tuple(reversed(graph.relations)), chunks)
        == planned
    )


def test_planner_rejects_duplicate_or_unowned_canonical_identities():
    document = make_document([DocumentElement("p1", "paragraph", "Canonical evidence.")])
    chunks = make_service(make_profile()).chunk(make_request(document)).chunks
    graph = GraphProjectionBuilder().build(
        GraphProjectionInput(
            document=document,
            chunks=chunks,
            resolved_targets={},
            graph_projection_version="graph-v1",
        )
    )
    with pytest.raises(ValueError, match="unique graph node"):
        summary_plan((*graph.nodes, graph.nodes[0]), graph.relations, chunks)
    with pytest.raises(ValueError, match="duplicate evidence"):
        summary_plan(graph.nodes, graph.relations, (*chunks, chunks[0]))
    with pytest.raises(ValueError, match="assign every evidence"):
        summary_plan(graph.nodes, (), chunks)


@pytest.mark.asyncio
async def test_reducer_rejects_impossible_budgets_nonprogress_and_invalid_citations(monkeypatch):
    cache, generator = Cache(), Generator()
    reducer = SummaryReducer("tenant", SummaryPolicy(model_fingerprint="model"), cache, generator)
    assert not reducer._fits(())
    assert not reducer._fits(("x" * 24001,))

    monkeypatch.setattr(reducer, "_fits", lambda _inputs: False)
    with pytest.raises(ValueError, match="fit one character"):
        reducer._split("x")

    reducer = SummaryReducer("tenant", SummaryPolicy(model_fingerprint="model"), cache, generator)
    with pytest.raises(ValueError, match="input exceeds"):
        await reducer._call(("x" * 24001,))

    class InvalidGenerator:
        async def generate(self, _packets):
            return DescriptionOutput(
                description="Invalid citations.",
                cited_packet_ids=("unknown",),
                complete=False,
            )

    reducer = SummaryReducer(
        "tenant", SummaryPolicy(model_fingerprint="model"), Cache(), InvalidGenerator()
    )
    with pytest.raises(ValueError, match="incomplete"):
        await reducer._call(("input",))

    reducer = SummaryReducer("tenant", SummaryPolicy(model_fingerprint="model"), Cache(), generator)
    monkeypatch.setattr(reducer, "_fits", lambda inputs: len(inputs) == 1)

    async def same_size(_inputs):
        return SummaryCard(description="Long enough replacement")

    monkeypatch.setattr(reducer, "_call", same_size)
    with pytest.raises(ValueError, match="cannot make progress"):
        await reducer.reduce("DataSource", {}, ("first", "second"))
