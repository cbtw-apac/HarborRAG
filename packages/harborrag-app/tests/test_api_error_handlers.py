"""Every HarborError maps to one enveloped HTTP response with a catalog code."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from harborrag_app.api.errors import register_error_handlers
from harborrag_core.contracts.errors import (
    HarborAuthError,
    HarborCapabilityError,
    HarborConfigurationError,
    HarborConflictError,
    HarborDeadlineExceeded,
    HarborError,
    HarborNoIndexedContentError,
    HarborNotFoundError,
    HarborRateLimitError,
    HarborSecretDecryptionError,
    HarborSecurityError,
    HarborUnavailableError,
    HarborValidationError,
)

CASES = [
    (HarborValidationError("bad"), 422, "validation_error"),
    (HarborNotFoundError("missing"), 404, "not_found"),
    (HarborConflictError("clash"), 409, "invalid_state"),
    (HarborNoIndexedContentError("empty"), 409, "no_indexed_content"),
    (HarborAuthError("no token"), 401, "unauthenticated"),
    (HarborAuthError("wrong role", forbidden=True), 403, "forbidden"),
    (HarborCapabilityError("nope"), 501, "not_supported"),
    (HarborSecurityError("denied"), 403, "forbidden"),
    (HarborDeadlineExceeded("slow"), 504, "deadline_exceeded"),
    (HarborConfigurationError("broken"), 500, "internal_error"),
    (HarborRateLimitError("slow down"), 429, "rate_limited"),
    (HarborUnavailableError("down"), 503, "dependency_unavailable"),
    (HarborSecretDecryptionError("cannot decrypt"), 500, "internal_error"),
]


def _app_raising(exc: Exception) -> TestClient:
    """Build a throwaway FastAPI app whose /boom route
    raises `exc` with the production error handlers registered."""
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/boom")
    def boom() -> None:
        raise exc

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.blackbox
@pytest.mark.parametrize(("exc", "status", "code"), CASES, ids=[c[2] + str(c[1]) for c in CASES])
def test_harbor_errors_are_enveloped(exc: HarborError, status: int, code: str) -> None:
    """Each HarborError subclass maps to its planned HTTP status and the standard
    {"error": {code, message, details, trace_id}} envelope."""
    response = _app_raising(exc).get("/boom")
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    assert body["error"]["message"]
    assert "details" in body["error"]
    assert body["error"]["trace_id"] == response.headers["x-trace-id"]


@pytest.mark.blackbox
def test_unhandled_exception_is_enveloped_generic_and_logged(caplog) -> None:
    """Non-Harbor Exceptions return an enveloped 500 whose message
    leaks no internal detail."""
    with caplog.at_level("ERROR", logger="harborrag.app.api.errors"):
        response = _app_raising(RuntimeError("secret internals")).get("/boom")
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert "secret internals" not in body["error"]["message"]
    assert "Unhandled API exception" in caplog.text
    assert "path=/boom" in caplog.text


@pytest.mark.blackbox
def test_rate_limit_error_includes_retry_after_header() -> None:
    response = _app_raising(HarborRateLimitError("slow down", retry_after_seconds=7)).get("/boom")

    assert response.status_code == 429
    assert response.headers["retry-after"] == "7"
    assert response.json()["error"]["details"] == {"retry_after_seconds": 7}
