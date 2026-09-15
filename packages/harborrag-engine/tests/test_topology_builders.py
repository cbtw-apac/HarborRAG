"""Pure builders retain exact evidence, independent representations, and all lineage."""

from __future__ import annotations

import asyncio
import json

import pytest

from harborrag_core.topology import (
    ChunkExtractionInput,
    ExtractionOutput,
    ExtractionProfile,
    TopologyJob,
    TopologyPolicy,
)
from harborrag_core.topology.derived import (
    ChunkEnrichment,
    ContextualIndexProfile,
    DescriptionOutput,
    DescriptionPacket,
    description_prompt_json,
)
from harborrag_core.topology.extraction import EvidenceSpan
from harborrag_engine.topology.build_builder import EnrichmentBuildBuilder
from harborrag_engine.topology.contextual_builder import ContextualRecordBuilder
from harborrag_engine.topology.parent_builder import (
    ParentDescriptionBuilder,
    ParentDescriptionPolicy,
)
from harborrag_engine.topology.vector_values import canonical_dense_vector


def job(tenant="tenant-1"):
    return TopologyJob(
        job_id="job",
        tenant_id=tenant,
        source_scope_id="scope",
        document_id="doc",
        document_version_id="version",
        policy_revision=1,
        state="running",
        policy=TopologyPolicy(
            tenant_id=tenant,
            source_scope_id="scope",
            enabled=True,
            profile=ExtractionProfile(
                model="low-cost", deployment_revision="pinned", prompt_digest="prompt"
            ),
        ),
    )


def profile():
    return ContextualIndexProfile(model="existing", dimension=2, deployment_revision="pinned")


def test_vector_storage_precision_is_frozen_once_and_rejects_overflow():
    quantized = canonical_dense_vector((0.1, 0.2))
    assert quantized != (0.1, 0.2)
    assert canonical_dense_vector(quantized) == quantized
    with pytest.raises(ValueError, match="float32"):
        canonical_dense_vector((1e40,))


def test_parent_prompt_revision_does_not_invalidate_independent_contextual_vectors():
    original = profile()
    changed = original.model_copy(update={"description_revision": "parent-new-prompt"})
    assert changed.fingerprint == original.fingerprint
    assert changed.index_name == original.index_name
    assert changed.parent_fingerprint != original.parent_fingerprint
    assert changed.parent_index_name != original.parent_index_name


def test_contextual_record_is_separate_stable_tenant_scoped_and_does_not_store_generated_text():
    builder = ContextualRecordBuilder(profile())
    value = ChunkExtractionInput(chunk_id="chunk", content="original evidence")
    first = builder.build(job(), "build", value, (0.6, 0.8))
    assert builder.text(value, "grounded context") == "grounded context\n\noriginal evidence"
    assert first == builder.build(job(), "build", value, (0.6, 0.8))
    assert first.id != builder.build(job("other"), "build", value, (0.6, 0.8)).id
    assert first.payload["record_kind"] == "contextual"
    assert first.payload["projection_point_id"] == first.id
    assert "content" not in first.payload
    assert "retrieval_context" not in first.payload
    assert value.content == "original evidence"


@pytest.mark.parametrize("vector", [(1.0,), (float("nan"), 0.0), (float("inf"), 0.0)])
def test_contextual_vectors_reject_wrong_dimension_and_nonfinite_values(vector):
    with pytest.raises(ValueError):
        ContextualRecordBuilder(profile()).build(
            job(), "build", ChunkExtractionInput(chunk_id="c", content="raw"), vector
        )


def test_contextual_text_limits_bytes_without_silent_truncation():
    builder = ContextualRecordBuilder(profile().model_copy(update={"max_input_bytes": 100}))
    with pytest.raises(ValueError, match="byte budget"):
        builder.text(ChunkExtractionInput(chunk_id="c", content="世" * 34), "context")


def test_parent_prompt_excludes_permission_and_citation_lineage_arrays():
    packet = DescriptionPacket(
        packet_id="packet",
        text="Bounded evidence description.",
        chunk_ids=tuple(f"chunk-{index}" for index in range(100)),
        cited_chunk_ids=("chunk-0",),
    )

    assert json.loads(description_prompt_json((packet,))) == [
        {"packet_id": "packet", "text": "Bounded evidence description."}
    ]


def test_build_refuses_incomplete_or_extra_input_coverage_and_forged_spans():
    builder = EnrichmentBuildBuilder(job())
    value = ChunkExtractionInput(chunk_id="chunk", content="Alpha")
    for inputs, outputs in (
        ((value,), {}),
        ((value, value), {"chunk": ExtractionOutput()}),
        ((value,), {"chunk": ExtractionOutput(), "extra": ExtractionOutput()}),
    ):
        with pytest.raises(ValueError, match="exactly one extraction"):
            builder.build(inputs, outputs, resolved={})
    forged = ExtractionOutput(
        description="generated", description_evidence=(EvidenceSpan(start=0, end=5, quote="wrong"),)
    )
    with pytest.raises(ValueError, match="evidence span"):
        builder.build((value,), {"chunk": forged}, resolved={})


def test_build_keeps_grounded_descriptions_and_section_path_without_rewriting_source():
    value = ChunkExtractionInput(chunk_id="chunk", content="Alpha", heading_path=("Heading",))
    output = ExtractionOutput(
        description="Alpha description",
        description_evidence=(EvidenceSpan(start=0, end=5, quote="Alpha"),),
    )
    result = EnrichmentBuildBuilder(job()).build((value,), {"chunk": output}, resolved={})
    assert result.chunk_ids == ("chunk",)
    assert result.representations[0].description == "Alpha description"
    assert result.representations[0].section_path == ("Heading",)
    assert result.representations[0].evidence[0].quote == "Alpha"
    assert value.content == "Alpha"


class Generator:
    def __init__(self):
        self.calls = []
        self.unknown = False

    async def generate(self, packets):
        self.calls.append(packets)
        await asyncio.sleep(0)
        return DescriptionOutput(
            description="qualified summary",
            cited_packet_ids=("unknown" if self.unknown else packets[0].packet_id,),
            complete=True,
        )


def chunks(count):
    return tuple(
        ChunkEnrichment(
            chunk_id=f"chunk-{i}", description=f"Evidence {i}", section_path=("Section",)
        )
        for i in range(count)
    )


@pytest.mark.asyncio
async def test_parent_synthesis_tracks_all_inputs_not_just_selected_citations():
    generator = Generator()
    parents = await ParentDescriptionBuilder(generator).build("doc", chunks(3))
    assert len(generator.calls) == 1
    assert parents[-1].input_chunk_ids == ("chunk-0", "chunk-1", "chunk-2")
    assert parents[-1].cited_chunk_ids == ("chunk-0",)
    assert parents[-1].description == "qualified summary"
    assert parents[-1].level == "document"


@pytest.mark.asyncio
async def test_parent_synthesis_materializes_every_section_prefix_bottom_up():
    generator = Generator()
    nested = (
        ChunkEnrichment(chunk_id="a", description="A evidence", section_path=("A",)),
        ChunkEnrichment(chunk_id="b", description="B evidence", section_path=("A", "B")),
        ChunkEnrichment(
            chunk_id="c",
            description="C evidence",
            section_path=("A", "B", "C"),
        ),
    )
    parents = await ParentDescriptionBuilder(generator).build("doc", nested)
    sections = {parent.section_path: parent for parent in parents if parent.level == "section"}
    assert set(sections) == {("A",), ("A", "B"), ("A", "B", "C")}
    assert sections[("A", "B", "C")].input_chunk_ids == ("c",)
    assert set(sections[("A", "B")].input_chunk_ids) == {"b", "c"}
    assert set(sections[("A",)].input_chunk_ids) == {"a", "b", "c"}
    assert set(parents[-1].input_chunk_ids) == {"a", "b", "c"}
    assert all(parent.description for parent in parents)


@pytest.mark.asyncio
async def test_parent_synthesis_does_not_merge_repeated_heading_labels():
    generator = Generator()
    repeated = (
        ChunkEnrichment(
            chunk_id="a",
            description="First status.",
            section_path=("Status",),
            section_ids=("section-a",),
        ),
        ChunkEnrichment(
            chunk_id="b",
            description="Second status.",
            section_path=("Status",),
            section_ids=("section-b",),
        ),
    )
    parents = await ParentDescriptionBuilder(generator).build("doc", repeated)
    sections = [parent for parent in parents if parent.level == "section"]

    assert len(sections) == 2
    assert {parent.structure_id for parent in sections} == {"section-a", "section-b"}
    assert len({parent.parent_key for parent in sections}) == 2
    assert {parent.section_path for parent in sections} == {("Status",)}


@pytest.mark.asyncio
async def test_shared_parent_builder_has_independent_per_document_call_budgets():
    generator = Generator()
    builder = ParentDescriptionBuilder(
        generator, ParentDescriptionPolicy(max_fan_in=2, max_calls=4)
    )
    left, right = await asyncio.gather(
        builder.build("left", chunks(5)), builder.build("right", chunks(5))
    )
    assert left[-1].input_chunk_ids == right[-1].input_chunk_ids
    assert len(generator.calls) == 8
    assert all(len(packets) <= 2 for packets in generator.calls)


@pytest.mark.asyncio
async def test_parent_reduction_never_exceeds_operation_call_budget():
    generator = Generator()
    with pytest.raises(ValueError, match="no provider calls were issued"):
        await ParentDescriptionBuilder(
            generator, ParentDescriptionPolicy(max_fan_in=2, max_calls=1)
        ).build("doc", chunks(5))
    assert not generator.calls


@pytest.mark.asyncio
async def test_parent_reduction_preflights_large_flat_scope_without_spending():
    generator = Generator()
    with pytest.raises(ValueError, match="requires 144 calls"):
        await ParentDescriptionBuilder(generator).build("doc", chunks(1000))
    assert not generator.calls


@pytest.mark.asyncio
async def test_parent_reduction_counts_non_ascii_input_conservatively_before_spending():
    generator = Generator()
    values = tuple(
        ChunkEnrichment(
            chunk_id=f"multilingual-{index}",
            description="界" * 400,
            section_path=("International",),
        )
        for index in range(2)
    )

    with pytest.raises(ValueError, match="input budget"):
        await ParentDescriptionBuilder(
            generator,
            ParentDescriptionPolicy(max_input_tokens=100),
        ).build("doc", values)

    assert not generator.calls


@pytest.mark.asyncio
async def test_missing_children_and_unknown_citations_never_publish_parent_description():
    generator = Generator()
    builder = ParentDescriptionBuilder(generator)
    with pytest.raises(ValueError, match="incomplete"):
        await builder.build("doc", (ChunkEnrichment(chunk_id="empty"),))
    assert not generator.calls
    generator.unknown = True
    with pytest.raises(ValueError, match="unknown packets"):
        await builder.build("doc", chunks(2))


@pytest.mark.asyncio
async def test_duplicate_child_identity_is_rejected_before_generation():
    generator = Generator()
    with pytest.raises(ValueError, match="duplicate"):
        await ParentDescriptionBuilder(generator).build("doc", (*chunks(1), *chunks(1)))
    assert not generator.calls
