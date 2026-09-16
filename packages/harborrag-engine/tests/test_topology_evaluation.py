import pytest

from harborrag_core.topology import (
    ChunkExtractionInput,
    EvidenceSpan,
    ExtractedAssertion,
    ExtractedEntity,
    ExtractionOutput,
)
from harborrag_core.topology.evaluation import TopologyEvaluationUnit
from harborrag_engine.topology.evaluation import evaluate_extractions


def unit() -> TopologyEvaluationUnit:
    entities = (
        ExtractedEntity(
            local_id="a",
            name="A",
            entity_type="service",
            span=EvidenceSpan(start=0, end=1, quote="A"),
        ),
        ExtractedEntity(
            local_id="b",
            name="B",
            entity_type="service",
            span=EvidenceSpan(start=13, end=14, quote="B"),
        ),
    )
    assertion = ExtractedAssertion(
        local_id="ab",
        subject_id="a",
        object_id="b",
        predicate="depends_on",
        span=EvidenceSpan(start=0, end=14, quote="A depends on B"),
    )
    expected = ExtractionOutput(entities=entities, assertions=(assertion,))
    return TopologyEvaluationUnit(
        document_id="d",
        input=ChunkExtractionInput(chunk_id="c", content="A depends on B"),
        expected=expected,
        predicted=expected,
    )


def test_perfect_toy_labels_score_one_without_a_corpus_quality_claim() -> None:
    report = evaluate_extractions([unit()])
    assert report.entities.precision == report.entities.recall == report.entities.f1 == 1
    assert report.assertions.true_positives == 1
    assert report.units == report.documents == 1
    assert report.by_split["test"].entities.expected == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("polarity", "negative"),
        ("modality", "possible"),
        ("time_qualifier", "2026"),
        ("subject_id", "b"),
    ],
)
def test_relation_qualifiers_and_direction_are_part_of_match(field: str, value: str) -> None:
    original = unit()
    changed = original.predicted.assertions[0].model_copy(update={field: value})
    if field == "subject_id":
        changed = changed.model_copy(update={"object_id": "a"})
    predicted = original.predicted.model_copy(update={"assertions": (changed,)})
    report = evaluate_extractions([original.model_copy(update={"predicted": predicted})])
    assert report.entities.f1 == 1
    assert report.assertions.true_positives == 0
    assert report.assertions.precision == report.assertions.recall == 0


def test_duplicate_predictions_and_invalid_spans_count_as_false_positives() -> None:
    original = unit()
    duplicate = original.predicted.entities[0].model_copy(update={"local_id": "duplicate"})
    invalid = original.predicted.entities[0].model_copy(
        update={
            "local_id": "invalid",
            "span": EvidenceSpan(start=0, end=999, quote="A depends on B"),
        }
    )
    predicted = original.predicted.model_copy(
        update={"entities": (*original.predicted.entities, duplicate, invalid)}
    )
    report = evaluate_extractions([original.model_copy(update={"predicted": predicted})])
    assert report.entities.true_positives == 2 and report.entities.predicted == 4
    assert report.entities.precision == 0.5 and report.entities.recall == 1
    assert report.invalid_evidence_spans == 1


def test_missing_predictions_empty_sets_and_invalid_gold_are_explicit() -> None:
    original = unit()
    report = evaluate_extractions([original.model_copy(update={"predicted": ExtractionOutput()})])
    assert report.entities.recall == 0 and report.entities.expected == 2
    assert evaluate_extractions([]).entities.f1 == 0
    changed = original.input.model_copy(update={"content": "wrong"})
    with pytest.raises(ValueError, match="evidence span"):
        evaluate_extractions([original.model_copy(update={"input": changed})])


def test_document_holdout_rejects_cross_split_leakage_and_duplicate_units() -> None:
    original = unit()
    with pytest.raises(ValueError, match="holdout"):
        evaluate_extractions([original, original.model_copy(update={"split": "train"})])
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_extractions([original, original])
