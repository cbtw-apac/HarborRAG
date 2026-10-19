"""Ingestion failure classification wired with adapter-level infrastructure errors."""

from __future__ import annotations

from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as ConnectionPoolTimeoutError

from harborrag_adapters.repositories.errors import (
    HarborStorageConnectionError,
    HarborStorageRateLimitError,
    HarborStorageTimeoutError,
)
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
    """Classifier that treats storage, database and network faults as transient."""

    return IngestionFailureClassifier(transient_errors=INFRASTRUCTURE_ERRORS)
