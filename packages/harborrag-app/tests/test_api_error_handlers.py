"""Every HarborError maps to one eveloped HTTP response."""

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
    HarborNotFoundError,
    HarborSecurityError,
    HarborValidationError,
)

CASES = [
    (HarborValidationError("bad"), 422, "harbor_validation_error"),
    (HarborNotFoundError("missing"), 404, "harbor_not_found_error"),
    (HarborConflictError("clash"), 409, "harbor_conflict_error"),
    (HarborAuthError("no token"), 401, "harbor_auth_error"),
    (HarborAuthError("wrong role", forbidden=True), 403, "harbor_auth_error"),
    (HarborCapabilityError("nope"), 501, "harbor_capability_error"),
    (HarborSecurityError("denied"), 403, "harbor_security_error"),
    (HarborDeadlineExceeded("slow"), 504, "harbor_deadline_exceeded"),
    (HarborConfigurationError("broken"), 500, "harbor_configuration_error"),
]


def _app_raising(exc: Exception) -> TestClient:
    """BUild a throwaway FastAPI app whose /boom route
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
    assert "trace_id" in body["error"]


@pytest.mark.blackbox
def test_unhandled_exception_is_enveloped_and_generic() -> None:
    """Non-Harbor Exceptions return an enveloped 500 whose message
    leaks no internal detail."""
    response = _app_raising(RuntimeError("secret internals")).get("/boom")
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert "secret internals" not in body["error"]["message"]
