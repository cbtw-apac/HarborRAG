"""Safe ingestion failure classification and Temporal retry intent."""

from __future__ import annotations

import pytest
from temporalio.exceptions import ApplicationError

from harborrag_adapters.connectors.exceptions import (
    AuthenticationError,
    FetchError,
)
from harborrag_adapters.parsers.errors import ParseError
from harborrag_core.contracts.errors import HarborConflictError
from harborrag_core.ingestion import FailureCategory
from harborrag_engine.ingestion import IngestionFailureClassifier
from harborrag_runtime.ingestion.observability import IngestionTelemetry
from harborrag_runtime.temporal.activity_observability import (
    ActivityObservability,
)
from harborrag_runtime.temporal.failure_handling import durable_failure

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


@pytest.mark.parametrize(
    ("stage", "error", "retryable"),
    [
        ("FetchAndCaptureRaw", AuthenticationError("denied"), False),
        ("FetchAndCaptureRaw", FetchError("temporary"), True),
        ("ParseAndNormalize", ParseError("invalid source"), False),
        ("ChunkAndValidate", ValueError("invalid chunk"), False),
        ("BuildRelations", HarborConflictError("immutable mismatch"), False),
        ("WriteVectorProjection", RuntimeError("unavailable"), True),
    ],
)
def test_failure_classifier_matches_activity_retry_matrix(
    stage: str,
    error: Exception,
    retryable: bool,
) -> None:
    failure = IngestionFailureClassifier().classify(stage, error)

    assert failure.retryable is retryable


def test_immutable_artifact_conflict_is_a_safe_validation_failure() -> None:
    failure = IngestionFailureClassifier().classify(
        "BuildRelations",
        HarborConflictError("private artifact key"),
    )

    assert failure.category == FailureCategory.CANONICAL_VALIDATION
    assert failure.code == "immutable_artifact_conflict"
    assert failure.retryable is False


def test_activity_boundary_emits_safe_non_retryable_application_error() -> None:
    observability = ActivityObservability(
        IngestionTelemetry(),
        failures=IngestionFailureClassifier(),
    )

    with pytest.raises(ApplicationError) as captured:
        with observability.boundary("ChunkAndValidate"):
            raise ValueError("source content that must not enter history")

    assert captured.value.non_retryable is True
    assert captured.value.type == "chunkandvalidate_valueerror"
    assert "source content" not in str(captured.value)


def test_durable_failure_uses_declared_safe_application_error_type() -> None:
    error = ApplicationError(
        "safe operator message",
        type="parser_format_unsupported",
        non_retryable=True,
    )

    failure_type, message = durable_failure(error)

    assert failure_type == "parser_format_unsupported"
    assert "inspect restricted worker logs" in message
