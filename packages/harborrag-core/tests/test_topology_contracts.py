import pytest
from pydantic import ValidationError

from harborrag_core.topology import (
    ChunkExtractionInput,
    EvidenceSpan,
    ExtractedAssertion,
    ExtractedEntity,
    ExtractionOutput,
    ExtractionProfile,
    TopologyPolicy,
)


def test_input_fingerprint_includes_context_but_not_version_bound_chunk_identity() -> None:
    original = ChunkExtractionInput(
        chunk_id="v1/chunk", content="Gateway", context="Payment service"
    )
    next_version = original.model_copy(update={"chunk_id": "v2/chunk"})
    changed_context = original.model_copy(update={"context": "Network infrastructure"})
    assert original.input_digest == next_version.input_digest
    assert original.input_digest != changed_context.input_digest


def test_extraction_and_resolution_revisions_are_independent() -> None:
    profile = ExtractionProfile(model="qwen", deployment_revision="weights-v1", prompt_digest="p1")
    policy = TopologyPolicy(tenant_id="t", source_scope_id="s", profile=profile)
    resolution = policy.model_copy(update={"resolution_revision": "r2"})
    model_change = profile.model_copy(update={"deployment_revision": "weights-v2"})
    assert resolution.fingerprint != policy.fingerprint
    assert resolution.profile.fingerprint == profile.fingerprint
    assert model_change.fingerprint != profile.fingerprint


@pytest.mark.parametrize("start,end,quote", [(0, 100, "Gateway"), (1, 7, "Gateway"), (5, 3, "a")])
def test_evidence_coordinates_cannot_overrun_or_disagree(start: int, end: int, quote: str) -> None:
    with pytest.raises(ValueError):
        EvidenceSpan(start=start, end=end, quote=quote).validate_content("Gateway")


def test_extraction_rejects_unknown_endpoints_and_duplicate_mentions() -> None:
    entity = ExtractedEntity(
        local_id="e1",
        name="Gateway",
        entity_type="service",
        span=EvidenceSpan(start=0, end=7, quote="Gateway"),
    )
    assertion = ExtractedAssertion(
        local_id="a1",
        subject_id="e1",
        object_id="missing",
        predicate="depends_on",
        span=entity.span,
    )
    with pytest.raises(ValidationError, match="unknown entity"):
        ExtractionOutput(entities=(entity,), assertions=(assertion,))
    with pytest.raises(ValidationError, match="duplicate entity"):
        ExtractionOutput(entities=(entity, entity))
    ExtractionOutput().validate_evidence(ChunkExtractionInput(chunk_id="c", content=""))


def test_polarity_and_modality_are_preserved_and_ontology_is_closed() -> None:
    assertion = ExtractedAssertion(
        local_id="a",
        subject_id="a",
        object_id="b",
        predicate="depends_on",
        polarity="negative",
        modality="possible",
        time_qualifier="since 2026",
        span=EvidenceSpan(start=0, end=1, quote="A"),
    )
    assert assertion.polarity == "negative" and assertion.modality == "possible"
    with pytest.raises(ValidationError):
        ExtractedAssertion.model_validate({**assertion.model_dump(), "predicate": "DROP DATABASE"})
