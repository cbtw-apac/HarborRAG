"""White-box unit tests for shared connector exception types."""

from __future__ import annotations

import pytest

from harborrag_adapters.connectors.exceptions import (
    AuthenticationError,
    AuthorizationError,
    ConnectorError,
    ConnectorNotFoundError,
    ConnectorNotInitializedError,
    DocumentProcessingError,
    FetchError,
    RateLimitError,
)

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


@pytest.mark.parametrize(
    "exc_type",
    [
        ConnectorNotFoundError,
        ConnectorNotInitializedError,
        AuthenticationError,
        AuthorizationError,
        FetchError,
        DocumentProcessingError,
    ],
)
def test_exceptions_subclass_connector_error(exc_type):
    assert issubclass(exc_type, ConnectorError)


def test_rate_limit_error_subclasses_fetch_error():
    assert issubclass(RateLimitError, FetchError)


@pytest.mark.parametrize(
    ("exc_type", "code"),
    [
        (AuthenticationError, 401),
        (AuthorizationError, 403),
    ],
)
def test_authentication_and_authorization_errors_carry_status_code(exc_type, code):
    """The raw HTTP status must be inspectable on the exception itself (not just
    baked into the message), so callers/logs can surface 401 vs 403 directly."""
    error = exc_type("denied", status_code=code)
    assert error.status_code == code


@pytest.mark.parametrize("exc_type", [AuthenticationError, AuthorizationError])
def test_authentication_and_authorization_errors_default_status_code_to_none(exc_type):
    error = exc_type("denied")
    assert error.status_code is None
