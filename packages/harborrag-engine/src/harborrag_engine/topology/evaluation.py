"""Deterministic exact-match evaluation; results describe only supplied labeled units."""

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from harborrag_core.topology import (
    EvidenceSpan,
    ExtractedAssertion,
    ExtractedEntity,
    ExtractionOutput,
)
from harborrag_core.topology.evaluation import (
    ExtractionMetrics,
    SplitEvaluation,
    TopologyEvaluationReport,
    TopologyEvaluationUnit,
)

type EntityKey = tuple[int, int, str]
type AssertionKey = tuple[EntityKey, EntityKey, str, str, str, str | None]


def valid_span(span: EvidenceSpan, content: str) -> bool:
    try:
        span.validate_content(content)
    except ValueError:
        return False
    return True


def entity_key(value: ExtractedEntity) -> EntityKey:
    return value.span.start, value.span.end, value.entity_type


def assertion_key(value: ExtractedAssertion, entities: dict[str, ExtractedEntity]) -> AssertionKey:
    return (
        entity_key(entities[value.subject_id]),
        entity_key(entities[value.object_id]),
        value.predicate,
        value.polarity,
        value.modality,
        value.time_qualifier,
    )


def observation_keys(
    value: ExtractionOutput,
    content: str,
) -> tuple[Counter[EntityKey], Counter[AssertionKey], int]:
    entities = {item.local_id: item for item in value.entities if valid_span(item.span, content)}
    valid_assertions = tuple(item for item in value.assertions if valid_span(item.span, content))
    assertions = Counter(
        assertion_key(item, entities)
        for item in valid_assertions
        if item.subject_id in entities and item.object_id in entities
    )
    invalid = len(value.entities) - len(entities) + len(value.assertions) - len(valid_assertions)
    return Counter(entity_key(item) for item in entities.values()), assertions, invalid


def metrics(counts: tuple[int, int, int]) -> ExtractionMetrics:
    true_positives, predicted, expected = counts
    # Empty denominators use zero, preserving a visible "no examples" count rather
    # than reporting perfect quality for an empty gold set.
    precision = true_positives / predicted if predicted else 0.0
    recall = true_positives / expected if expected else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return ExtractionMetrics(
        true_positives=true_positives,
        predicted=predicted,
        expected=expected,
        precision=precision,
        recall=recall,
        f1=f1,
    )


def add_counts(left: tuple[int, int, int], right: tuple[int, int, int]) -> tuple[int, int, int]:
    return left[0] + right[0], left[1] + right[1], left[2] + right[2]


@dataclass
class EvaluationCounts:
    units: int = 0
    documents: set[str] = field(default_factory=set)
    entity_counts: tuple[int, int, int] = (0, 0, 0)
    assertion_counts: tuple[int, int, int] = (0, 0, 0)
    invalid: int = 0

    def add(self, unit: TopologyEvaluationUnit) -> None:
        unit.expected.validate_evidence(unit.input)
        expected_entities, expected_assertions, _ = observation_keys(
            unit.expected, unit.input.content
        )
        predicted_entities, predicted_assertions, invalid = observation_keys(
            unit.predicted, unit.input.content
        )
        entity_tp = sum((expected_entities & predicted_entities).values())
        assertion_tp = sum((expected_assertions & predicted_assertions).values())
        entity_counts = (entity_tp, len(unit.predicted.entities), len(unit.expected.entities))
        assertion_counts = (
            assertion_tp,
            len(unit.predicted.assertions),
            len(unit.expected.assertions),
        )
        self.entity_counts = add_counts(self.entity_counts, entity_counts)
        self.assertion_counts = add_counts(self.assertion_counts, assertion_counts)
        self.units += 1
        self.documents.add(unit.document_id)
        self.invalid += invalid

    def report(self) -> SplitEvaluation:
        return SplitEvaluation(
            units=self.units,
            documents=len(self.documents),
            entities=metrics(self.entity_counts),
            assertions=metrics(self.assertion_counts),
            invalid_evidence_spans=self.invalid,
        )


def evaluate_extractions(units: Sequence[TopologyEvaluationUnit]) -> TopologyEvaluationReport:
    """Micro-average supplied units and report per split; enforce document holdout."""
    if len(units) > 100000:
        raise ValueError("evaluation exceeds the 100000-unit budget")
    documents: dict[str, str] = {}
    identities: set[tuple[str, str]] = set()
    totals = EvaluationCounts()
    splits: dict[str, EvaluationCounts] = defaultdict(EvaluationCounts)
    for unit in units:
        previous = documents.setdefault(unit.document_id, unit.split)
        if previous != unit.split:
            raise ValueError("document holdout violated: one document appears in multiple splits")
        identity = (unit.document_id, unit.input.chunk_id)
        if identity in identities:
            raise ValueError("duplicate document/chunk evaluation unit")
        identities.add(identity)
        totals.add(unit)
        splits[unit.split].add(unit)
    return TopologyEvaluationReport(
        **totals.report().model_dump(),
        by_split={name: value.report() for name, value in sorted(splits.items())},
    )
