from __future__ import annotations

from dataclasses import replace

import pytest

from harborrag_core.chunking import ConnectorType, DocumentKind, TableProjectionType
from harborrag_engine.ingestion.chunking import (
    MatrixProjectionMode,
    TableChunkingPolicy,
    TableChunkingRequest,
    TableClassificationThresholds,
)
from harborrag_engine.ingestion.chunking.table.errors import TableChunkingError
from harborrag_engine.ingestion.chunking.table.fragmentation import TokenBudgetFragmenter
from harborrag_engine.ingestion.chunking.table.grouping import (
    RowProjectionPlan,
    TableRowGroupPlanner,
)
from harborrag_engine.ingestion.chunking.table.models import (
    TableClassification,
    TableQualityMetrics,
    TableShape,
)
from harborrag_engine.ingestion.chunking.table.rendering import TableRenderer

from .table_test_fixtures import CharacterTokenCounter, make_artifact, make_plan, make_request

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


@pytest.mark.parametrize(
    "changes",
    [
        {"small_maximum_rows": 0},
        {"small_maximum_columns": 0},
        {"small_maximum_tokens": 0},
        {"large_minimum_tokens": 0},
    ],
)
def test_classification_threshold_counts_must_be_positive(changes):
    with pytest.raises(ValueError, match="positive"):
        TableClassificationThresholds(**changes)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"long_minimum_rows": 50}, "long_minimum_rows"),
        ({"wide_minimum_columns": 10}, "wide_minimum_columns"),
        ({"matrix_confidence": -0.1}, "matrix_confidence"),
        ({"matrix_confidence": 1.1}, "matrix_confidence"),
        ({"time_series_confidence": -0.1}, "time_series_confidence"),
        ({"time_series_confidence": 1.1}, "time_series_confidence"),
    ],
)
def test_classification_threshold_relationships_are_validated(changes, message):
    with pytest.raises(ValueError, match=message):
        TableClassificationThresholds(**changes)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"target_rows_per_chunk": 0}, "positive"),
        (
            {"target_rows_per_chunk": 31, "maximum_rows_per_chunk": 30},
            "must not exceed",
        ),
        ({"boundary_row_overlap": 2}, "zero or one"),
        ({"key_column_confidence": -0.1}, "between zero and one"),
        ({"key_column_confidence": 1.1}, "between zero and one"),
        ({"explicit_key_columns": ("Service", "Service")}, "duplicates"),
    ],
)
def test_table_chunking_policy_rejects_unsafe_configuration(changes, message):
    with pytest.raises(ValueError, match=message):
        TableChunkingPolicy(**changes)


def test_table_model_contracts_are_immutable_and_validate_context():
    artifact = make_artifact(["Service", "CPU"], [["worker", "2"]])
    request = make_request(artifact)
    classification = TableClassification(
        shape=TableShape.SMALL,
        confidence=0.9,
        estimated_tokens=10,
        key_column_indices=(0,),
        key_column_confidences={0: 0.9},
    )
    metrics = TableQualityMetrics(
        boundary_score=1,
        self_containment_score=0.75,
        header_completeness_score=1,
        provenance_score=1,
        noise_score=0,
    )

    assert request.permissions["groups"] == ("engineering",)
    assert classification.key_column_confidences[0] == 0.9
    assert metrics.score == pytest.approx(0.95)
    assert MatrixProjectionMode.BOTH.value == "both"
    with pytest.raises(ValueError, match="confidence/tokens"):
        replace(classification, confidence=1.1)
    with pytest.raises(ValueError, match="confidence/tokens"):
        replace(classification, estimated_tokens=-1)
    with pytest.raises(ValueError, match="context"):
        TableChunkingRequest(
            artifact=artifact,
            tenant_id="",
            connection_id="connection",
            source_scope_id="scope",
            document_title="page",
            connector_type=ConnectorType.CONFLUENCE,
            document_kind=DocumentKind.CONFLUENCE_PAGE,
            source_context={"space": "space"},
        )


def test_fragmenter_rejects_a_prefix_that_exhausts_the_hard_budget():
    with pytest.raises(TableChunkingError, match="exhaust"):
        TokenBudgetFragmenter(_CharacterCounter()).split(
            "Header\nvalue",
            "context",
            1,
            repeat_header=True,
        )


def test_row_group_planner_returns_no_evidence_for_header_only_table():
    artifact = make_artifact(["Service", "CPU"], [])
    table_policy = make_plan().table_policy
    assert table_policy is not None

    groups = TableRowGroupPlanner(CharacterTokenCounter()).plan(
        artifact,
        TableRenderer(artifact),
        table_policy,
        RowProjectionPlan(
            columns=(0, 1),
            key_columns=(0,),
            projection_type=TableProjectionType.ROWS,
        ),
    )

    assert groups == []


class _CharacterCounter:
    def count(self, text: str) -> int:
        return len(text)
