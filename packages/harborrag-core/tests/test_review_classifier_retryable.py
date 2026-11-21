from __future__ import annotations

import pytest

from harborrag_core.ingestion import (
    FailureCategory,
    IngestionFailureClassifier,
    SourceForbiddenError,
    SourceUnavailableError,
)
from harborrag_core.models.errors import (
    HarborEmbedAuthenticationError,
    HarborEmbedInvalidRequestError,
    HarborEmbedRateLimitError,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "error",
    [
        HarborEmbedInvalidRequestError("input rejected", status_code=400),
        HarborEmbedAuthenticationError("key rejected", status_code=401),
    ],
)
def test_classifier_permanent_model_error_is_not_retried(error: Exception) -> None:
    failure = IngestionFailureClassifier().classify("EncodeChunks", error)

    assert failure.retryable is False
    assert failure.category is FailureCategory.ENCODER_FAILURE
    assert failure.code == f"encodechunks_{type(error).__name__.lower()}"


def test_classifier_transient_model_error_is_still_retried() -> None:
    error = HarborEmbedRateLimitError("slow down", status_code=429)

    assert IngestionFailureClassifier().classify("EncodeChunks", error).retryable is True


def test_classifier_known_failure_honours_a_false_retryable_flag() -> None:
    error = SourceUnavailableError("request rejected")
    error.retryable = False

    failure = IngestionFailureClassifier().classify("FetchAndCaptureRaw", error)

    assert failure.retryable is False
    assert failure.category is FailureCategory.TRANSIENT
    assert failure.code == "source_unavailable"


def test_classifier_retryable_flag_never_makes_a_permanent_failure_retryable() -> None:
    error = SourceForbiddenError("denied")
    error.retryable = True

    assert IngestionFailureClassifier().classify("FetchAndCaptureRaw", error).retryable is False
