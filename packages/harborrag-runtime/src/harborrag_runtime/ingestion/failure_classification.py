"""Ingestion failure classification wired with adapter-level infrastructure errors."""

from __future__ import annotations

from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as ConnectionPoolTimeoutError

from harborrag_adapters.repositories.errors import (
    HarborStorageConnectionError,
    HarborStorageRateLimitError,
    HarborStorageTimeoutError,
)
from harborrag_core.ingestion import FailureCategory
from harborrag_engine.ingestion import IngestionFailureClassifier


def _object_store_errors() -> tuple[type[BaseException], ...]:
    # botocore ships with the optional S3 extra. Its ClientError is what an
    # unmapped object-store response raises -- XMinioStorageFull, SlowDown,
    # InternalError -- and its connection errors cover an unreachable endpoint.
    try:
        from botocore.exceptions import ClientError, HTTPClientError  # type: ignore
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
    Temporal parse activity records, so direct and Temporal runs report it alike.
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
        ),
    )
