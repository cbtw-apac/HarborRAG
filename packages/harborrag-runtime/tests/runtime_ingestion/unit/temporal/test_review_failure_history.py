"""Failure codes and retry intent for permanent errors, and what reaches Temporal history."""

from __future__ import annotations

import pytest
from temporalio.api.failure.v1 import Failure
from temporalio.converter import DefaultFailureConverter, DefaultPayloadConverter
from temporalio.exceptions import ApplicationError

from harborrag_adapters.connectors.exceptions import FetchError, ItemNotFoundError, RateLimitError
from harborrag_adapters.connectors.policies.network import BlockedDestinationError
from harborrag_adapters.repositories.errors import (
    HarborVectorDimensionError,
    StorageErrorContext,
    StorageFailure,
    storage_failure_error,
)
from harborrag_core.ingestion import FailureCategory, SourceItemNotFoundError
from harborrag_core.models.errors import (
    HarborEmbedAuthenticationError,
    HarborEmbedInvalidRequestError,
)
from harborrag_core.storage import StorageFamily
from harborrag_runtime.ingestion.failure_classification import ingestion_failure_classifier
from harborrag_runtime.ingestion.observability import IngestionTelemetry
from harborrag_runtime.temporal.activity_observability import ActivityObservability
from harborrag_runtime.temporal.failure_handling import durable_failure, terminal_failure_code

pytestmark = pytest.mark.unit

SYNTHETIC_NAME = "quarterly-plan.pdf"


def test_blocked_destination_is_a_non_retryable_configuration_failure() -> None:
    error = BlockedDestinationError("wiki.internal resolves to the private address 10.0.0.5")

    failure = ingestion_failure_classifier().classify("FetchAndCaptureRaw", error)

    assert failure.category is FailureCategory.INVALID_CONFIGURATION
    assert failure.retryable is False
    assert failure.code == "destination_blocked"


def _storage_context(family: StorageFamily) -> StorageErrorContext:
    return StorageErrorContext(
        family=family, backend="demo", instance_name="default", operation="write"
    )


@pytest.mark.parametrize(
    ("stage", "error"),
    [
        (
            "WriteVectorProjection",
            storage_failure_error(
                StorageFailure.REJECTED, _storage_context(StorageFamily.VECTOR), status_code=400
            ),
        ),
        (
            "WriteVectorProjection",
            HarborVectorDimensionError(
                "vector size differs from the collection",
                context=_storage_context(StorageFamily.VECTOR),
            ),
        ),
        (
            "FetchAndCaptureRaw",
            storage_failure_error(
                StorageFailure.REJECTED,
                _storage_context(StorageFamily.OBJECT_STORE),
                status_code=400,
            ),
        ),
    ],
    ids=["qdrant-400", "vector-dimension", "s3-400"],
)
def test_a_request_the_store_rejects_is_not_retried(stage: str, error: Exception) -> None:
    failure = ingestion_failure_classifier().classify(stage, error)

    assert failure.category is FailureCategory.INVALID_CONFIGURATION
    assert failure.retryable is False
    assert failure.code == "storage_request_rejected"


def test_a_storage_outage_is_still_retried() -> None:
    error = storage_failure_error(
        StorageFailure.UNAVAILABLE, _storage_context(StorageFamily.VECTOR), status_code=503
    )

    assert ingestion_failure_classifier().classify("WriteVectorProjection", error).retryable


@pytest.mark.parametrize("status_code", [400, 403, 409, 422])
def test_permanent_client_error_is_not_retried(status_code: int) -> None:
    error = FetchError(f"request failed with HTTP {status_code}", status_code=status_code)

    failure = ingestion_failure_classifier().classify("DiscoverSourceItems", error)

    assert failure.retryable is False


@pytest.mark.parametrize(
    "error",
    [
        FetchError("request timed out", status_code=408),
        RateLimitError("slow down", status_code=429),
        FetchError("bad gateway", status_code=502),
        FetchError("connection reset"),
    ],
)
def test_transient_fetch_error_is_still_retried(error: FetchError) -> None:
    assert ingestion_failure_classifier().classify("DiscoverSourceItems", error).retryable is True


def test_item_not_found_keeps_its_not_found_code() -> None:
    error = ItemNotFoundError("request failed with HTTP 404", status_code=404)

    failure = ingestion_failure_classifier().classify("FetchAndCaptureRaw", error)

    assert failure.code == "source_item_not_found"
    assert failure.retryable is False


@pytest.mark.parametrize(
    "error",
    [
        HarborEmbedInvalidRequestError("input rejected", status_code=400),
        HarborEmbedAuthenticationError("key rejected", status_code=401),
    ],
)
def test_permanent_embed_error_is_not_retried(error: Exception) -> None:
    assert ingestion_failure_classifier().classify("EncodeChunks", error).retryable is False


def test_activity_boundary_keeps_the_original_error_out_of_temporal_history() -> None:
    error = SourceItemNotFoundError(f"{SYNTHETIC_NAME} was deleted")
    observability = ActivityObservability(IngestionTelemetry())

    with pytest.raises(ApplicationError) as captured:
        with observability.boundary("FetchAndCaptureRaw"):
            raise error

    application_error = captured.value
    assert application_error.__cause__ is None
    assert application_error.__suppress_context__ is True
    encoded = Failure()
    DefaultFailureConverter().to_failure(application_error, DefaultPayloadConverter(), encoded)
    assert SYNTHETIC_NAME not in str(encoded)
    assert not encoded.HasField("cause")
    assert encoded.application_failure_info.type == "source_item_not_found"
    assert encoded.application_failure_info.non_retryable is True


def test_activity_boundary_failure_still_records_its_code() -> None:
    observability = ActivityObservability(IngestionTelemetry())

    with pytest.raises(ApplicationError) as captured:
        with observability.boundary("FetchAndCaptureRaw"):
            raise BlockedDestinationError("wiki.internal resolves to the private address 10.0.0.5")

    assert terminal_failure_code(captured.value) == "destination_blocked"
    assert durable_failure(captured.value)[0] == "destination_blocked"
