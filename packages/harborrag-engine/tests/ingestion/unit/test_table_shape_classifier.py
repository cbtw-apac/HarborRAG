from __future__ import annotations

import pytest

from harborrag_engine.ingestion.chunking import (
    TableShape,
    TableShapeClassifier,
)

from .table_test_fixtures import (
    CharacterTokenCounter,
    make_artifact,
    make_plan,
)

pytestmark = [pytest.mark.unit, pytest.mark.blackbox]


@pytest.mark.parametrize(
    ("headers", "rows", "expected"),
    [
        (["Service", "CPU"], [["worker", "2"]], TableShape.SMALL),
        (
            ["Service", "CPU"],
            [["a", "1"], ["b", "2"], ["c", "3"], ["d", "4"]],
            TableShape.LONG,
        ),
        (
            ["Service", "CPU", "RAM", "Retry"],
            [["worker", "2", "4", "3"]],
            TableShape.WIDE,
        ),
        (
            ["Service", "CPU"],
            [[f"service-{index}", str(index)] for index in range(9)],
            TableShape.LARGE,
        ),
        (
            ["Region", "Jan", "Feb"],
            [["APAC", "10", "12"], ["EMEA", "8", "9"]],
            TableShape.MATRIX,
        ),
        (
            ["Timestamp", "Service", "Latency (ms)"],
            [
                ["2025-01-01T00:00:00Z", "api", "20"],
                ["2025-01-02T00:00:00Z", "api", "25"],
            ],
            TableShape.TIME_SERIES,
        ),
    ],
)
def test_classifier_supports_every_table_shape(headers, rows, expected):
    artifact = make_artifact(headers, rows)
    plan = make_plan()

    result = TableShapeClassifier(CharacterTokenCounter()).classify(
        artifact,
        plan.table_policy,
    )

    assert result.shape == expected
    assert 0 <= result.confidence <= 1
    assert result.estimated_tokens > 0


def test_classification_precedence_is_large_then_time_then_matrix_then_width():
    classifier = TableShapeClassifier(CharacterTokenCounter())
    large_time = make_artifact(
        ["Timestamp", "Value"],
        [[f"2025-01-{index + 1:02d}", str(index)] for index in range(9)],
    )
    time_wide = make_artifact(
        ["Timestamp", "A", "B", "C"],
        [["2025-01-01", "1", "2", "3"], ["2025-01-02", "2", "3", "4"]],
    )

    assert classifier.classify(large_time, make_plan().table_policy).shape == TableShape.LARGE
    assert classifier.classify(time_wide, make_plan().table_policy).shape == TableShape.TIME_SERIES


def test_key_column_selection_honors_explicit_configuration_and_warns_on_unknown_names():
    artifact = make_artifact(
        ["Code", "Description", "Value"],
        [["A", "first item", "1"], ["B", "second item", "2"]],
    )
    plan = make_plan(explicit_key_columns=("Code", "Missing"))

    result = TableShapeClassifier(CharacterTokenCounter()).classify(
        artifact,
        plan.table_policy,
    )

    assert result.key_column_indices[0] == 0
    assert result.key_column_confidences[0] == 1.0
    assert result.warnings == ("configured key column does not exist: Missing",)
