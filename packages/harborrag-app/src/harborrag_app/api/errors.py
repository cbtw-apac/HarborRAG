"""Map the HarborError hierarchy onto enveloped HTTP responses.

One handler, one envelope: {"error": {"code", "message", "details", "trace_id"}}.
Codes are derived mechanically from the exception class name (CamelCase -> snake_case),
so new HarborError subclasses only need a row in _STATUS_BY_TYPE.
"""

from __future__ import annotations

import re
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from harborrag_core.contracts.errors import (
    HarborAuthError,
    HarborCapabilityError,
    HarborConfigurationError,
    HarborConflictError,
    HarborDeadlineExceeded,
    HarborError,
    HarborNotFoundError,
    HarborSecurityError,
    HarborValidationError,
)
from starlette.exceptions import HTTPException as StarletteHTTPException

_STATUS_BY_TYPE: dict[type[HarborError], int] = {
    HarborValidationError: 422,
    HarborNotFoundError: 404,
    HarborConflictError: 409,
    HarborCapabilityError: 501,
    HarborSecurityError: 403,
    HarborDeadlineExceeded: 504,
    HarborConfigurationError: 500,
}


def _code_for(exc: Exception) -> str:
    """Derive the wire error code from the exception class name.

    CamelCase -> snake_case, e.g., HarborNotFoundError -> harbor_not_found_error.
    """
    return re.sub(r"(?<!^)(?=[A-Z])", "_", type(exc).__name__).lower()


def _status_for(exc: HarborError) -> int:
    """Resolve the HTTP status for a HarborError.

    HarborAuthError fans out to 401 (unauthenticated) or 403 (forbidden);
    every other class walks its MRO so subclasses inherit their parent's
    status without needing their own _STATUS_BY_TYPE row. Unknown branches
    of the hierarchy default to 500."""

    if isinstance(exc, HarborAuthError):
        return 403 if exc.forbidden else 401
    for klass in type(exc).__mro__:
        if klass in _STATUS_BY_TYPE:
            return _STATUS_BY_TYPE[klass]
    return 500


def error_envelope(
    request: Request,
    code: str,
    message: str,
    details: dict[str, object],
) -> dict[str, object]:
    """Build the single error envelope every non-2xx response uses.

    trace_id is read from request.state (populated by the trace middleware);
    it is present-but-null until that middleware is wired in.
    """
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details,
            "trace_id": getattr(request.state, "trace_id", None),
        }
    }


def register_error_handlers(app: FastAPI) -> None:
    """Attach the three exception handlers to a FastAPI app.

    HarborError -> mapped status + mechanical code; RequestValidationError ->
    422 with FastAPI's field errors as details; anything else -> generic 500
    that leaks no internal detail (full traceback belongs in logs only)."""

    @app.exception_handler(HarborError)
    async def _harbor_error(request: Request, exc: HarborError) -> JSONResponse:
        """Envelope any HarborError with its mapped HTTP status."""
        details = getattr(exc, "details", {})
        return JSONResponse(
            status_code=_status_for(exc),
            content=error_envelope(request, _code_for(exc), str(exc), details),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Envelope FastAPI request-validation failures as 422."""
        details: dict[str, object] = {"errors": exc.errors()}
        return JSONResponse(
            status_code=422,
            content=error_envelope(
                request, "harbor_validation_error", "Request validation failed", details
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """Envelope framework HTTPExceptions (404 no-route, 405, ...) too —
        no response may bypass the envelope (ST3 DoD)."""
        code = HTTPStatus(exc.status_code).name.lower()
        return JSONResponse(
            status_code=exc.status_code,
            content=error_envelope(request, code, str(exc.detail), {}),
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        """Envelope unexpected exceptions as a generic 500."""
        return JSONResponse(
            status_code=500,
            content=error_envelope(
                request, "internal_error", "Internal server error", {}
            ),
        )
