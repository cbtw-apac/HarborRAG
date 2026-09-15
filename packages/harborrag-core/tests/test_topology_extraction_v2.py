"""Fused output provenance, ontology safety and complete deterministic coverage."""

import pytest
from pydantic import ValidationError

from harborrag_core.topology.extraction import (
    ChunkExtractionInput,
    EvidenceSpan,
    ExtractedAssertion,
    ExtractedEntity,
    ExtractionOutput,
    ExtractionProfile,
    digest,
)
from harborrag_core.topology.ontology import OntologyRegistry, RelationDefinition, builtin_ontology
from harborrag_core.topology.windowing import extraction_windows, merge_window_outputs


def profile(**changes):
    value = ExtractionProfile(
        model="test",
        deployment_revision="r2",
        prompt_digest="p2",
        schema_version="2",
        ontology_version="builtin-enterprise-v1",
        ontology=builtin_ontology(),
        context_policy="chunk-v2",
        code_version="2",
        max_input_chars=100,
    )
    return value.model_copy(update=changes)


def output(content="AB", *, entity_type="service"):
    span = EvidenceSpan(start=0, end=len(content), quote=content)
    return ExtractionOutput(
        title="Services",
        description="A service observation",
        retrieval_context="Service details",
        title_evidence=(span,),
        description_evidence=(span,),
        retrieval_context_evidence=(span,),
        entities=tuple(
            ExtractedEntity(local_id=name, name=name, entity_type=entity_type, span=span)
            for name in ("A", "B")
        ),
        assertions=(
            ExtractedAssertion(
                local_id="claim",
                subject_id="A",
                object_id="B",
                predicate="depends_on",
                span=span,
                statement_text="A may not depend on B after next quarter, unless approved.",
                polarity="negative",
                modality="possible",
                attribution="Team C",
                qualifiers=("unless approved",),
                valid_from="next quarter",
                temporal_precision="relative",
            ),
        ),
    )


def test_legacy_artifacts_and_profile_input_keys_remain_readable():
    legacy = ExtractionOutput.model_validate({"entities": [], "assertions": []})
    assert legacy.title == "" and legacy.complete and not legacy.overflow
    old_profile = ExtractionProfile(model="m", deployment_revision="r", prompt_digest="p")
    old_payload = old_profile.model_dump(mode="json", exclude={"ontology", "max_windows"})
    assert old_profile.fingerprint == digest(old_payload)
    original = ChunkExtractionInput(chunk_id="id", content="AB")
    assert original.input_digest == digest({"content": "AB", "context": ""})
    legacy.validate_profile(old_profile)


def test_fused_fields_and_qualified_meaning_survive_json_roundtrip():
    value = output()
    restored = ExtractionOutput.model_validate_json(value.model_dump_json())
    restored.validate_evidence(ChunkExtractionInput(chunk_id="c", content="AB"))
    restored.validate_profile(profile())
    claim = restored.assertions[0]
    assert (claim.polarity, claim.modality, claim.attribution, claim.qualifiers) == (
        "negative",
        "possible",
        "Team C",
        ("unless approved",),
    )
    assert claim.valid_from == "next quarter" and claim.temporal_precision == "relative"
    assert "unless approved" in claim.statement_text


@pytest.mark.parametrize(
    "precision,start,end",
    [
        ("day", "2026-02-30", None),
        ("month", "2026-1", None),
        ("year", "2027", "2026"),
        ("instant", "2026-01-01T12:00:00", None),
        ("instant", "2026-01-02T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    ],
)
def test_qualified_assertions_reject_invalid_dates_or_reversed_intervals(precision, start, end):
    payload = output().assertions[0].model_dump()
    with pytest.raises(ValidationError):
        ExtractedAssertion.model_validate(
            {**payload, "temporal_precision": precision, "valid_from": start, "valid_to": end}
        )


@pytest.mark.parametrize("field", ["title", "description", "retrieval_context"])
def test_generated_text_requires_exact_grounding(field):
    value = output().model_copy(update={field + "_evidence": ()})
    with pytest.raises(ValueError, match="grounded"):
        value.validate_evidence(ChunkExtractionInput(chunk_id="c", content="AB"))
    value = output().model_copy(
        update={field + "_evidence": (EvidenceSpan(start=0, end=2, quote="ZZ"),)}
    )
    with pytest.raises(ValueError, match="span does not match"):
        value.validate_evidence(ChunkExtractionInput(chunk_id="c", content="AB"))


def test_context_can_ground_generated_text_but_not_entity_assertions():
    span = EvidenceSpan(start=0, end=7, quote="Product", source="context")
    value = output().model_copy(update={"retrieval_context_evidence": (span,)})
    source = ChunkExtractionInput(chunk_id="c", content="AB", context="Product")
    value.validate_evidence(source)
    claim = value.assertions[0].model_copy(update={"span": span})
    with pytest.raises(ValueError, match="target chunk"):
        value.model_copy(update={"assertions": (claim,)}).validate_evidence(source)


@pytest.mark.parametrize("changes", [{"complete": False}, {"overflow": True}])
def test_incomplete_output_is_never_ready(changes):
    with pytest.raises(ValueError, match="incomplete or overflow"):
        output().model_copy(update=changes).validate_profile(profile())


def test_ontology_rejects_unknown_entities_predicates_and_reversed_types():
    with pytest.raises(ValueError, match="entity type"):
        output(entity_type="invented").validate_profile(profile())
    claim = output().assertions[0]
    for predicate in ("invented", "owns"):
        with pytest.raises(ValueError, match="ontology"):
            output().model_copy(
                update={"assertions": (claim.model_copy(update={"predicate": predicate}),)}
            ).validate_profile(profile())


def test_frozen_configured_ontology_has_explicit_directional_compatibility():
    registry = OntologyRegistry(
        version="acme@1",
        entity_types=("person", "asset"),
        relations=(RelationDefinition(name="maintains", endpoint_pairs=(("person", "asset"),)),),
    )
    with pytest.raises(ValidationError):
        registry.version = "changed"
    with pytest.raises(ValueError, match="undefined"):
        registry.model_validate({**registry.model_dump(), "entity_types": ("person",)})
    assert (
        profile(ontology=registry, ontology_version="acme@1").fingerprint != profile().fingerprint
    )
    with pytest.raises(ValueError, match="version"):
        profile(ontology=registry).resolved_ontology()


def test_windows_cover_unicode_once_preserve_identity_and_map_evidence():
    original = ChunkExtractionInput(
        chunk_id="original-chunk",
        content="é" * 140 + "尾" * 60,
        context="source context",
        source_revision="v2",
    )
    windows = extraction_windows(original, profile())
    assert len(windows) == 3
    assert "".join(window.input.content for window in windows) == original.content
    assert all(window.input.chunk_id == original.chunk_id for window in windows)
    assert extraction_windows(original, profile()) == windows
    outputs = {window.window_id: output(window.input.content) for window in windows}
    merged = merge_window_outputs(original, windows, outputs)
    merged.validate_evidence(original)
    assert len({entity.local_id for entity in merged.entities}) == 6
    assert {claim.span.start for claim in merged.assertions} == {window.start for window in windows}
    assert all(
        claim.polarity == "negative" and claim.qualifiers == ("unless approved",)
        for claim in merged.assertions
    )


def test_window_checkpoint_keys_include_position_and_context_lineage():
    value = ChunkExtractionInput(chunk_id="c", content="a" * 200)
    first, second = extraction_windows(value, profile())
    assert first.input.content == second.input.content
    assert first.input.input_digest != second.input.input_digest
    changed = value.model_copy(update={"context_dependencies": ("rev2:policy2",)})
    assert extraction_windows(changed, profile())[0].input.input_digest != first.input.input_digest


def test_missing_overlapping_or_overflow_windows_cannot_be_accepted():
    value = ChunkExtractionInput(chunk_id="c", content="a" * 200)
    windows = extraction_windows(value, profile())
    outputs = {window.window_id: output(window.input.content) for window in windows}
    with pytest.raises(ValueError, match="cover"):
        merge_window_outputs(value, windows, {})
    bad = (windows[0], windows[1].model_copy(update={"start": 90}))
    with pytest.raises(ValueError, match="coverage"):
        merge_window_outputs(value, bad, outputs)
    outputs[windows[1].window_id] = outputs[windows[1].window_id].model_copy(
        update={"overflow": True}
    )
    with pytest.raises(ValueError, match="overflow"):
        merge_window_outputs(value, windows, outputs)
    with pytest.raises(ValueError, match="call budget"):
        extraction_windows(value, profile(max_windows=1))


def test_semantic_v4_rejects_scalar_or_sentence_entities_and_long_prose():
    span = EvidenceSpan(start=0, end=2, quote="AB")
    value = output().model_copy(
        update={
            "retrieval_context": "",
            "retrieval_context_evidence": (),
            "entities": tuple(
                entity.model_copy(
                    update={"description": "Named service.", "description_evidence": (span,)}
                )
                for entity in output().entities
            ),
        }
    )
    semantic_v4 = profile(
        schema_version="4",
        context_policy="chunk-v4-description-prefix",
        code_version="10",
    )
    value.validate_profile(semantic_v4)
    numeric = value.entities[0].model_copy(update={"name": "2026"})
    with pytest.raises(ValueError, match="numeric value"):
        value.model_copy(update={"entities": (numeric, *value.entities[1:])}).validate_profile(
            semantic_v4
        )
    sentence = value.entities[0].model_copy(update={"name": "This is an entire sentence."})
    with pytest.raises(ValueError, match="noun phrase"):
        value.model_copy(update={"entities": (sentence, *value.entities[1:])}).validate_profile(
            semantic_v4
        )
    with pytest.raises(ValueError, match="50-word"):
        value.model_copy(update={"description": "word " * 51}).validate_profile(semantic_v4)
