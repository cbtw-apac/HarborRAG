"""Output policy rejects candidates that break the graph text limits."""

from __future__ import annotations

import pytest

from harborrag_core.topology.extraction import (
    ChunkExtractionInput,
    EvidenceSpan,
    ExtractedAssertion,
    ExtractedEntity,
    ExtractionOutput,
)
from harborrag_core.topology.output_policy import ExtractionOutputPolicy


def output():
    return ExtractionOutput(
        title="Alpha",
        description="Alpha service",
        title_evidence=(EvidenceSpan(start=0, end=5, quote="Alpha"),),
        description_evidence=(EvidenceSpan(start=0, end=5, quote="Alpha"),),
        entities=(
            ExtractedEntity(
                local_id="e1",
                name="Alpha",
                entity_type="service",
                description="Alpha is a service.",
                description_evidence=(EvidenceSpan(start=0, end=5, quote="Alpha"),),
                span=EvidenceSpan(start=0, end=5, quote="Alpha"),
            ),
        ),
    )


def test_output_policy_rejects_overflow_and_invalid_candidates_without_mutating_them():
    original = output()
    valid = original.entities[0].model_copy(
        update={
            "description": "word " * 60,
            "aliases": ("Alpha Service", "123", "This is a sentence."),
        }
    )
    invalid = valid.model_copy(update={"local_id": "bad", "name": "42"})
    assertion = ExtractedAssertion(
        local_id="a1",
        subject_id="e1",
        object_id="bad",
        predicate="related_to",
        span=EvidenceSpan(start=0, end=5, quote="Alpha"),
        statement_text="word " * 60,
    )
    candidate = original.model_copy(
        update={
            "title": "123",
            "description": "word " * 60,
            "entities": (valid, invalid),
            "assertions": (assertion,),
        }
    )
    with pytest.raises(ValueError, match=r"4 violation\(s\).+invalid_endpoint"):
        ExtractionOutputPolicy().apply(
            candidate,
            ChunkExtractionInput(chunk_id="c", content="Alpha", heading_path=("Useful heading",)),
        )
    assert candidate.description.endswith("word ")
    assert len(candidate.entities) == 2 and len(candidate.assertions) == 1
