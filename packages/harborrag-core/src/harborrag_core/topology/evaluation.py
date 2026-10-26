"""Offline extraction evaluation input and transparent count-based metrics."""

from typing import Literal

from pydantic import Field

from harborrag_core.base import StrictModel

from .extraction import ChunkExtractionInput, ExtractionOutput


class TopologyEvaluationUnit(StrictModel):
    document_id: str = Field(min_length=1, max_length=128)
    split: Literal["train", "dev", "test"] = "test"
    input: ChunkExtractionInput
    expected: ExtractionOutput
    predicted: ExtractionOutput


class ExtractionMetrics(StrictModel):
    true_positives: int = Field(ge=0)
    predicted: int = Field(ge=0)
    expected: int = Field(ge=0)
    precision: float = Field(ge=0, le=1)
    recall: float = Field(ge=0, le=1)
    f1: float = Field(ge=0, le=1)


class SplitEvaluation(StrictModel):
    units: int = Field(ge=0)
    documents: int = Field(ge=0)
    entities: ExtractionMetrics
    assertions: ExtractionMetrics
    invalid_evidence_spans: int = Field(ge=0)


class TopologyEvaluationReport(SplitEvaluation):
    by_split: dict[str, SplitEvaluation]
