"""Ingestion failure classification wired with adapter-level infrastructure errors."""

from __future__ import annotations

from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as ConnectionPoolTimeoutError

from harborrag_adapters.connectors.policies.network import BlockedDestinationError
from harborrag_adapters.repositories.errors import (
    HarborStorageConnectionError,
    HarborStorageRateLimitError,
    HarborStorageTimeoutError,
    HarborStorageValidationError,
)
from harborrag_core.ingestion import FailureCategory, IngestionFailureClassifier


def _object_store_errors() -> tuple[type[BaseException], ...]:
    """Return the botocore errors an object store raises, empty without the S3 extra.

    ``ClientError`` is what an unmapped object-store response raises, such as
    ``XMinioStorageFull``, ``SlowDown`` or ``InternalError``. The connection
    errors cover an unreachable endpoint.
    """
    try:
        from botocore.exceptions import (  # type: ignore[import-untyped]  # no stubs
            ClientError,
            HTTPClientError,
        )
        from botocore.exceptions import ConnectionError as EndpointError
    except ImportError:
        return ()
    return (ClientError, HTTPClientError, EndpointError)


INFRASTRUCTURE_ERRORS: tuple[type[BaseException], ...] = (
    HarborStorageConnectionError,
    HarborStorageTimeoutError,
    HarborStorageRateLimitError,
    OperationalError,
    InterfaceError,
    ConnectionPoolTimeoutError,
    *_object_store_errors(),
)


def ingestion_failure_classifier() -> IngestionFailureClassifier:
    """Classifier that treats storage, database and network faults as transient.

    A pinned PDF parser the worker cannot build is ``parser_unavailable``, the code the
    Temporal parse activity records, so direct and Temporal runs report it alike. A
    connector request to a private address the operator has not allowed is
    ``destination_blocked`` and is not retried. A request a store rejects, such as a
    Qdrant or S3 400, is ``storage_request_rejected`` and is not retried either.
    """
    from harborrag_runtime.ingestion.document.run_tools import (  # noqa: PLC0415 - the document package imports this module
        ParserUnavailableError,
    )

    return IngestionFailureClassifier(
        transient_errors=INFRASTRUCTURE_ERRORS,
        known_failures=(
            (
                ParserUnavailableError,
                FailureCategory.INVALID_CONFIGURATION,
                True,
                "parser_unavailable",
            ),
            (
                BlockedDestinationError,
                FailureCategory.INVALID_CONFIGURATION,
                False,
                "destination_blocked",
            ),
            (
                HarborStorageValidationError,
                FailureCategory.INVALID_CONFIGURATION,
                False,
                "storage_request_rejected",
            ),
        ),
    )
