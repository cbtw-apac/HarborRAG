"""White-box unit tests for shared connector exception types."""

from __future__ import annotations

import pytest

from harborrag_adapters.connectors.exceptions import (
    AuthenticationError,
    ConnectorError,
    ConnectorNotFoundError,
    ConnectorNotInitializedError,
    DocumentProcessingError,
    FetchError,
    HTTPRequestError,
    RateLimitError,
)

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


@pytest.mark.parametrize(
    "exc_type",
    [
        ConnectorNotFoundError,
        ConnectorNotInitializedError,
        AuthenticationError,
        FetchError,
        DocumentProcessingError,
    ],
)
def test_exceptions_subclass_connector_error(exc_type):
    assert issubclass(exc_type, ConnectorError)


def test_rate_limit_error_subclasses_fetch_error():
    assert issubclass(RateLimitError, FetchError)


def test_http_request_error_captures_context_and_message():
    error = HTTPRequestError("https://example.com", status_code=503, message="down")
    assert error.url == "https://example.com"
    assert error.status_code == 503
    assert error.message == "down"
    assert str(error) == "HTTP request failed for https://example.com: down"


def test_http_request_error_defaults_message_when_omitted():
    error = HTTPRequestError("https://example.com")
    assert error.status_code is None
    assert error.message is None
    assert str(error) == "HTTP request failed for https://example.com: Unknown error"
