"""The error catalog and the handlers that answer every failure with one envelope.

Handles:
- Mapping ``HarborError`` classes, request validation, framework HTTP errors and
  unhandled exceptions onto ``{"error": {"code", "message", "details", "trace_id"}}``
- Choosing each response's ``code`` from the catalog, so clients branch on stable strings
- Naming the dependency when an untranslated connection failure reaches the handler

Does NOT decide what a service raises; the services own their exceptions.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any, NamedTuple

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from harborrag_core.contracts.errors import (
    HarborAuthError,
    HarborCapabilityError,
    HarborConfigurationError,
    HarborConflictError,
    HarborConnectionError,
    HarborDeadlineExceeded,
    HarborError,
    HarborNoIndexedContentError,
    HarborNotFoundError,
    HarborPreconditionFailedError,
    HarborRateLimitError,
    HarborSecretDecryptionError,
    HarborSecurityError,
    HarborUnavailableError,
    HarborValidationError,
)

from .dependency_failures import failed_dependency
from .middleware import TRACE_HEADER, current_trace_id
from .schemas import Error

logger = logging.getLogger("harborrag.app.api.errors")

CATALOG: Mapping[str, int] = {
    "tenant_required": 400,
    "unauthenticated": 401,
    "forbidden": 403,
    "tenant_forbidden": 403,
    "not_found": 404,
    "already_exists": 409,
    "already_running": 409,
    "session_busy": 409,
    "in_use": 409,
    "immutable_field": 409,
    "invalid_state": 409,
    "no_indexed_content": 409,
    "reindex_required": 409,
    "precondition_failed": 412,
    "payload_too_large": 413,
    "validation_error": 422,
    "reference_not_found": 422,
    "mode_unavailable": 422,
    "connection_out_of_scope": 422,
    "rate_limited": 429,
    "internal_error": 500,
    "not_supported": 501,
    "llm_unavailable": 503,
    "dependency_unavailable": 503,
    "not_ready": 503,
    "deadline_exceeded": 504,
}
"""Every ``code`` a response may carry, with its HTTP status."""

_DEFAULT_CODES: Mapping[int, str] = {
    400: "validation_error",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "not_found",
    409: "invalid_state",
    412: "precondition_failed",
    413: "payload_too_large",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    501: "not_supported",
    503: "dependency_unavailable",
    504: "deadline_exceeded",
}

_OPAQUE_ERRORS = (HarborConfigurationError, HarborSecretDecryptionError)
_INTERNAL_MESSAGE = "the server could not complete the request"
_AUTHENTICATE = 'Bearer realm="harborrag"'


class TenantRequiredError(HarborError):
    """The credential holds several tenants and the request names none."""

    error_code = "tenant_required"


class ReferenceNotFoundError(HarborValidationError):
    """An ID in the body or query does not exist or is not visible to the caller."""

    error_code = "reference_not_found"


class ReindexRequiredError(HarborConflictError):
    """The change needs chunks re-embedded or re-chunked; `reindex=true` lands in v2.1."""

    error_code = "reindex_required"


_STATUS_BY_TYPE: dict[type[HarborError], int] = {
    TenantRequiredError: 400,
    HarborPreconditionFailedError: 412,
    HarborValidationError: 422,
    HarborNotFoundError: 404,
    HarborNoIndexedContentError: 409,
    HarborConflictError: 409,
    HarborCapabilityError: 501,
    HarborSecurityError: 403,
    HarborDeadlineExceeded: 504,
    HarborConfigurationError: 500,
    HarborConnectionError: 503,
    HarborRateLimitError: 429,
    HarborUnavailableError: 503,
    HarborSecretDecryptionError: 500,
}


def documented_error_responses(
    descriptions: Mapping[int, str],
) -> dict[int | str, dict[str, Any]]:
    """Build OpenAPI entries for error responses that use the shared ``Error`` schema."""
    return {
        status_code: {"model": Error, "description": description}
        for status_code, description in descriptions.items()
    }


def _status_for(exc: HarborError) -> int:
    """Resolve the HTTP status of a ``HarborError`` from its class."""
    if isinstance(exc, HarborAuthError):
        return 403 if exc.forbidden else 401
    for klass in type(exc).__mro__:
        if klass in _STATUS_BY_TYPE:
            return _STATUS_BY_TYPE[klass]
    return 500


def catalog_code(status: int, explicit: object = None) -> str:
    """Return ``explicit`` when the catalog lists it for ``status``, else the status default.

    Args:
        status: The response's HTTP status.
        explicit: The code the exception asked for, if any.

    Returns:
        A code from ``CATALOG``.
    """
    if isinstance(explicit, str) and CATALOG.get(explicit) == status:
        return explicit
    return _DEFAULT_CODES.get(status, "internal_error" if status >= 500 else "validation_error")


def _code_for(exc: HarborError, status: int) -> str:
    """The catalog code of a ``HarborError``."""
    if isinstance(exc, HarborNoIndexedContentError):
        return "no_indexed_content"
    return catalog_code(status, getattr(exc, "error_code", None))


def _details_for(exc: HarborError, status: int) -> object:
    """Field errors as ``[{field, issue}]``; other details as given, ``None`` when empty."""
    if isinstance(exc, _OPAQUE_ERRORS):
        return None
    details = getattr(exc, "details", None)
    if not isinstance(exc, HarborValidationError) or _code_for(exc, status) != "validation_error":
        return details or None
    errors = details.get("errors") if isinstance(details, dict) else None
    if isinstance(errors, list) and all(
        isinstance(item, dict) and "field" in item for item in errors
    ):
        return errors
    field = details.get("field") if isinstance(details, dict) else None
    return [{"field": field if isinstance(field, str) else "", "issue": str(exc)}]


def error_envelope(request: Request, code: str, message: str, details: object) -> dict[str, object]:
    """Build the error envelope for ``request``."""
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or None,
            "trace_id": current_trace_id(request),
        }
    }


class _Body(NamedTuple):
    code: str
    message: str
    details: object = None


def _respond(
    request: Request,
    status: int,
    body: _Body,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Answer with the envelope and the trace header, whichever middleware runs."""
    response_headers = {TRACE_HEADER: current_trace_id(request), **(headers or {})}
    if status == HTTPStatus.UNAUTHORIZED:
        response_headers.setdefault("WWW-Authenticate", _AUTHENTICATE)
    return JSONResponse(
        status_code=status,
        content=error_envelope(request, *body),
        headers=response_headers,
    )


def spec_error_body(request: Request, exc: HarborError) -> dict[str, object]:
    """Return the error object for ``exc`` without raising it, e.g. inside a 2xx body."""
    status = _status_for(exc)
    message = _INTERNAL_MESSAGE if isinstance(exc, _OPAQUE_ERRORS) else str(exc)
    return error_envelope(request, _code_for(exc, status), message, _details_for(exc, status))[
        "error"
    ]  # type: ignore[return-value]


def _field_of(location: tuple[object, ...], kind: str) -> str:
    """The dotted field a validation error names; ``body`` for an unparsable body."""
    if kind == "json_invalid":
        return "body"
    parts = [str(part) for part in location if part not in {"body", "query", "path", "header"}]
    return ".".join(parts) or "body"


def _validation_details(exc: RequestValidationError) -> list[dict[str, str]]:
    """Every failing field of a request as ``{field, issue}``."""
    return [
        {"field": _field_of(tuple(error["loc"]), str(error.get("type"))), "issue": error["msg"]}
        for error in exc.errors()
    ]


def register_error_handlers(app: FastAPI) -> None:
    """Attach the handlers that turn every failure into the error envelope."""

    @app.exception_handler(HarborError)
    async def _harbor_error(request: Request, exc: HarborError) -> JSONResponse:
        """Envelope a ``HarborError`` with its mapped status and catalog code."""
        status = _status_for(exc)
        if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
            logger.exception("%s returned as %s", type(exc).__name__, status)
        message = _INTERNAL_MESSAGE if isinstance(exc, _OPAQUE_ERRORS) else str(exc)
        details = _details_for(exc, status)
        headers: dict[str, str] = {}
        if isinstance(exc, HarborRateLimitError):
            headers["Retry-After"] = str(exc.details["retry_after_seconds"])
        if (
            isinstance(exc, (HarborConnectionError, HarborUnavailableError))
            and not details
            and (dependency := failed_dependency(exc)) is not None
        ):
            details = {"dependency": dependency}
        return _respond(request, status, _Body(_code_for(exc, status), message, details), headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Envelope request validation failures as ``422 validation_error``."""
        body = _Body("validation_error", "Request validation failed", _validation_details(exc))
        return _respond(request, 422, body)

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Envelope framework HTTP errors such as an unknown route or a wrong method."""
        body = _Body(catalog_code(exc.status_code), str(exc.detail))
        return _respond(request, exc.status_code, body, exc.headers)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        """Envelope a lost dependency as ``503``, anything else as a generic ``500``."""
        dependency = failed_dependency(exc)
        logger.exception(
            "Unhandled API exception trace_id=%s method=%s path=%s",
            current_trace_id(request),
            request.method,
            request.url.path,
            exc_info=exc,
        )
        if dependency is not None:
            body = _Body(
                "dependency_unavailable", f"{dependency} is unavailable", {"dependency": dependency}
            )
            return _respond(request, 503, body)
        return _respond(request, 500, _Body("internal_error", "Internal server error"))


__all__ = [
    "CATALOG",
    "ReferenceNotFoundError",
    "TenantRequiredError",
    "catalog_code",
    "documented_error_responses",
    "error_envelope",
    "register_error_handlers",
    "spec_error_body",
]
