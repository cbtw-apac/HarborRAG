"""The handlers that answer every failure with one envelope.

Handles:
- Mapping ``HarborError`` classes, request validation, framework HTTP errors and
  unhandled exceptions onto ``{"error": {"code", "message", "details", "trace_id"}}``
- Choosing each response's ``code`` from the catalog, so clients branch on stable strings

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

from harborrag_app.operations.errors import catalog_code, error_body
from harborrag_core.contracts.errors import HarborError, HarborRateLimitError

from .middleware import TRACE_HEADER, current_trace_id
from .schemas import Error

logger = logging.getLogger("harborrag.app.api.errors")

_AUTHENTICATE = 'Bearer realm="harborrag"'


def documented_error_responses(
    descriptions: Mapping[int, str],
) -> dict[int | str, dict[str, Any]]:
    """Build OpenAPI entries for error responses that use the shared ``Error`` schema."""
    return {
        status_code: {"model": Error, "description": description}
        for status_code, description in descriptions.items()
    }


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
    body = error_body(exc)
    return error_envelope(request, body.code, body.message, body.details)["error"]  # type: ignore[return-value]


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
        body = error_body(exc)
        if body.status >= HTTPStatus.INTERNAL_SERVER_ERROR:
            logger.exception("%s returned as %s", type(exc).__name__, body.status)
        headers: dict[str, str] = {}
        if isinstance(exc, HarborRateLimitError):
            headers["Retry-After"] = str(exc.details["retry_after_seconds"])
        return _respond(request, body.status, _Body(body.code, body.message, body.details), headers)

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
        logger.exception(
            "Unhandled API exception trace_id=%s method=%s path=%s",
            current_trace_id(request),
            request.method,
            request.url.path,
            exc_info=exc,
        )
        body = error_body(exc)
        return _respond(request, body.status, _Body(body.code, body.message, body.details))
