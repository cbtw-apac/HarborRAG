"""Translate Qdrant client errors, over HTTP and gRPC, into HarborRAG storage errors.

Only outages, timeouts, rate limits and rejected requests are translated. Any other
client error, and every HarborRAG error, passes through unchanged.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, cast

import httpx

from harborrag_adapters.repositories.errors import (
    HarborStorageError,
    StorageErrorContext,
    StorageFailure,
    storage_failure_error,
)

_ResponseHandlingException: Any
_UnexpectedResponse: Any
_RpcError: Any
try:
    from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
except ImportError:  # pragma: no cover - optional dependency
    _ResponseHandlingException = _UnexpectedResponse = None
else:
    _ResponseHandlingException = ResponseHandlingException
    _UnexpectedResponse = UnexpectedResponse
try:
    import grpc  # type: ignore[import-untyped]  # grpcio ships no type stubs
except ImportError:  # pragma: no cover - installed with qdrant-client
    _RpcError = None
else:
    _RpcError = grpc.RpcError

_VENDOR_ERRORS: tuple[type[Exception], ...] = tuple(
    error for error in (_ResponseHandlingException, _UnexpectedResponse, _RpcError) if error
)
_HTTP_FAILURES = {
    400: StorageFailure.REJECTED,
    422: StorageFailure.REJECTED,
    429: StorageFailure.RATE_LIMITED,
    500: StorageFailure.UNAVAILABLE,
    502: StorageFailure.UNAVAILABLE,
    503: StorageFailure.UNAVAILABLE,
    504: StorageFailure.UNAVAILABLE,
}
_GRPC_FAILURES = {
    "UNAVAILABLE": StorageFailure.UNAVAILABLE,
    "DEADLINE_EXCEEDED": StorageFailure.TIMEOUT,
    "INVALID_ARGUMENT": StorageFailure.REJECTED,
}


def qdrant_storage_error(
    error: Exception, context: StorageErrorContext
) -> HarborStorageError | None:
    """Return the storage error a Qdrant client error stands for.

    Args:
        error: Error the Qdrant client raised.
        context: Context of the failed operation.

    Returns:
        The storage error to raise from ``error``, or ``None`` to re-raise ``error``.
    """
    if _ResponseHandlingException is not None and isinstance(error, _ResponseHandlingException):
        timed_out = isinstance(getattr(error, "source", None), httpx.TimeoutException)
        failure = StorageFailure.TIMEOUT if timed_out else StorageFailure.UNAVAILABLE
        return storage_failure_error(failure, context)
    if _UnexpectedResponse is not None and isinstance(error, _UnexpectedResponse):
        http_failure = _HTTP_FAILURES.get(error.status_code)
        if http_failure is None:
            return None
        return storage_failure_error(http_failure, context, status_code=error.status_code)
    code = _grpc_code(error)
    grpc_failure = _GRPC_FAILURES.get(code) if code is not None else None
    if grpc_failure is None:
        return None
    return storage_failure_error(grpc_failure, context, status_code=code)


def _grpc_code(error: Exception) -> str | None:
    """The gRPC status name of ``error``, such as ``UNAVAILABLE``, if it has one."""
    if _RpcError is None or not isinstance(error, _RpcError):
        return None
    code = getattr(error, "code", None)
    status = code() if callable(code) else None
    name = getattr(status, "name", None)
    return name if isinstance(name, str) else None


def translate_qdrant_errors[Operation: Callable[..., Awaitable[Any]]](
    operation: Operation,
) -> Operation:
    """Translate the Qdrant client errors a repository operation raises.

    The decorated method's instance must have ``_queries`` and take a keyword ``context``.
    """

    @wraps(operation)
    async def translated(self: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return await operation(self, *args, **kwargs)
        except _VENDOR_ERRORS as error:
            context = self._queries.error_context(operation.__name__, context=kwargs.get("context"))
            storage_error = qdrant_storage_error(error, context)
            if storage_error is None:
                raise
            raise storage_error from error

    return cast(Operation, translated)
