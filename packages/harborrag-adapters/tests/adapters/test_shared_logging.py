from __future__ import annotations

import logging

import pytest

from harborrag_adapters._shared.logging import log_adapter_failure, vendor_status_code
from harborrag_core.observability.log_events import FailureKind

pytestmark = pytest.mark.unit

LOGGER = logging.getLogger("harborrag.adapters.test_shared_logging")


class _Response:
    def __init__(self, status_code: object) -> None:
        self.status_code = status_code


class _VendorHTTPError(Exception):
    """A vendor HTTP error that keeps its response, as requests and httpx errors do."""

    def __init__(self, message: str, status_code: object) -> None:
        super().__init__(message)
        self.response = _Response(status_code)


@pytest.mark.parametrize(
    ("status", "expected"), [(429, 429), (None, None), ("401", None), (True, None), (999, None)]
)
def test_only_a_real_http_status_on_the_response_is_read(
    status: object, expected: int | None
) -> None:
    assert vendor_status_code(_VendorHTTPError("x", status)) == expected


def test_an_adapter_failure_names_its_backend_and_status_but_not_the_error_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER.name)
    error = _VendorHTTPError("429 for https://example.atlassian.net/browse/DEMO-1", 429)

    log_adapter_failure(
        LOGGER, "connector.request_failed", error, "jira", FailureKind.CLASSIFIED, item_id="DEMO-1"
    )

    (record,) = caplog.records
    assert record.getMessage() == (
        "connector.request_failed error_type=_VendorHTTPError backend=jira status_code=429 item_id=DEMO-1"
    )
    assert (
        record.funcName
        == "test_an_adapter_failure_names_its_backend_and_status_but_not_the_error_text"
    )
    assert record.exc_info is None
