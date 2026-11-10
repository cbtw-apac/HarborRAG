"""The error catalogue and the one mapping from an exception to a status, code and message.

Handles:
- Mapping ``HarborError`` classes and unhandled exceptions onto ``ErrorBody``
- Choosing each body's ``code`` from the catalogue, so clients branch on stable strings
- Naming the dependency when an untranslated connection failure arrives

Does NOT import a web framework: the HTTP handlers and the CLI both call ``error_body``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from harborrag_app.operations.dependency_failures import failed_dependency
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

DEFAULT_CODES: Mapping[int, str] = {
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

OPAQUE_ERRORS = (HarborConfigurationError, HarborSecretDecryptionError)
INTERNAL_MESSAGE = "the server could not complete the request"


class TenantRequiredError(HarborError):
    """The credential holds several tenants and the request names none."""

    error_code = "tenant_required"


class ReferenceNotFoundError(HarborValidationError):
    """An ID in the body or query does not exist or is not visible to the caller."""

    error_code = "reference_not_found"


class TenantForbiddenError(HarborAuthError):
    """The request names a tenant the credential does not hold."""

    error_code = "tenant_forbidden"

    def __init__(self, message: str = "the credential does not hold this tenant") -> None:
        super().__init__(message, forbidden=True)


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


@dataclass(frozen=True, slots=True)
class ErrorBody:
    """A failure as a transport reports it.

    Args:
        status: HTTP status, which the CLI maps onto its exit code.
        code: Stable string from ``CATALOG``.
        message: Client-safe text; opaque for configuration and secret failures.
        details: ``[{field, issue}]``, ``{"dependency": name}``, other details, or ``None``.
    """

    status: int
    code: str
    message: str
    details: object = None


def status_for(exc: HarborError) -> int:
    """Resolve the HTTP status of a ``HarborError`` from its class."""
    if isinstance(exc, HarborAuthError):
        return 403 if exc.forbidden else 401
    for klass in type(exc).__mro__:
        if klass in _STATUS_BY_TYPE:
            return _STATUS_BY_TYPE[klass]
    return 500


def catalog_code(status: int, explicit: object = None) -> str:
    """Return ``explicit`` when the catalogue lists it for ``status``, else the status default.

    Args:
        status: The response's HTTP status.
        explicit: The code the exception asked for, if any.

    Returns:
        A code from ``CATALOG``.
    """
    if isinstance(explicit, str) and CATALOG.get(explicit) == status:
        return explicit
    return DEFAULT_CODES.get(status, "internal_error" if status >= 500 else "validation_error")


def code_for(exc: HarborError, status: int) -> str:
    """The catalogue code of a ``HarborError``."""
    if isinstance(exc, HarborNoIndexedContentError):
        return "no_indexed_content"
    return catalog_code(status, getattr(exc, "error_code", None))


def details_for(exc: HarborError, status: int) -> object:
    """Field errors as ``[{field, issue}]``; other details as given, ``None`` when empty."""
    if isinstance(exc, OPAQUE_ERRORS):
        return None
    details = getattr(exc, "details", None)
    if not isinstance(exc, HarborValidationError) or code_for(exc, status) != "validation_error":
        return details or None
    errors = details.get("errors") if isinstance(details, dict) else None
    if isinstance(errors, list) and all(
        isinstance(item, dict) and "field" in item for item in errors
    ):
        return errors
    field = details.get("field") if isinstance(details, dict) else None
    return [{"field": field if isinstance(field, str) else "", "issue": str(exc)}]


def error_body(exc: BaseException) -> ErrorBody:
    """Map any exception onto the status, code, message and details a transport reports.

    A ``HarborError`` keeps its class's status and catalogue code. Any other exception
    is ``503 dependency_unavailable`` when it is a lost connection to a named dependency,
    else ``500 internal_error`` with a generic message.

    Args:
        exc: The exception a use case raised.

    Returns:
        The body, free of the exception's own text for opaque and unknown failures.
    """
    if isinstance(exc, HarborError):
        status = status_for(exc)
        message = INTERNAL_MESSAGE if isinstance(exc, OPAQUE_ERRORS) else str(exc)
        details = details_for(exc, status)
        if (
            isinstance(exc, (HarborConnectionError, HarborUnavailableError))
            and not details
            and (dependency := failed_dependency(exc)) is not None
        ):
            details = {"dependency": dependency}
        return ErrorBody(status, code_for(exc, status), message, details)
    if (dependency := failed_dependency(exc)) is not None:
        return ErrorBody(
            503,
            "dependency_unavailable",
            f"{dependency} is unavailable",
            {"dependency": dependency},
        )
    return ErrorBody(500, "internal_error", "Internal server error")
