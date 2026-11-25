"""A failure's category agrees with its retry flag."""

from __future__ import annotations

import pytest

from harborrag_core.ingestion import (
    FailureCategory,
    IngestionFailureClassifier,
    SourceRetriesExhaustedError,
    SourceUnavailableError,
)
from harborrag_core.models.errors import HarborEmbedInvalidRequestError

pytestmark = pytest.mark.unit


class _PermanentSourceError(SourceUnavailableError):
    """A source answer that asking again cannot change, such as HTTP 400."""

    retryable = False


def test_permanent_source_error_is_invalid_configuration_and_keeps_its_code() -> None:
    failure = IngestionFailureClassifier().classify("FetchAndCaptureRaw", _PermanentSourceError())

    assert failure.category is FailureCategory.INVALID_CONFIGURATION
    assert failure.retryable is False
    assert failure.code == "source_unavailable"


def test_transient_source_error_stays_transient() -> None:
    failure = IngestionFailureClassifier().classify("FetchAndCaptureRaw", SourceUnavailableError())

    assert failure.category is FailureCategory.TRANSIENT
    assert failure.retryable is True


def test_exhausted_source_retries_stay_transient() -> None:
    failure = IngestionFailureClassifier().classify(
        "FetchAndCaptureRaw", SourceRetriesExhaustedError()
    )

    assert failure.category is FailureCategory.TRANSIENT
    assert failure.retryable is False


def test_permanent_model_error_keeps_its_stage_category() -> None:
    error = HarborEmbedInvalidRequestError("input rejected", status_code=400)

    failure = IngestionFailureClassifier().classify("EncodeChunks", error)

    assert failure.category is FailureCategory.ENCODER_FAILURE
