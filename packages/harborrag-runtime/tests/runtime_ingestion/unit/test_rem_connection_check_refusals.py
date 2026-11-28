"""A connection test reports a refused 4xx as forbidden, not as an unreachable source."""

from __future__ import annotations

import pytest

from harborrag_adapters.connectors.exceptions import FetchError, RefusedResponseError
from harborrag_runtime.ingestion.connection_checks import classify

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("status_code", [400, 410, 422])
def test_classify_a_refused_request_reports_forbidden(status_code: int) -> None:
    check, category, message = classify(
        FetchError("bad JQL", status_code=status_code), default="preview"
    )

    assert (check, category) == ("permissions", "forbidden")
    assert message == f"{status_code} from the source: the source refused the request"


@pytest.mark.parametrize("status_code", [408, 500, 503])
def test_classify_a_retryable_answer_stays_unreachable(status_code: int) -> None:
    check, category, _ = classify(FetchError("busy", status_code=status_code), default="preview")

    assert (check, category) == ("preview", "unreachable")


def test_classify_a_refused_response_stays_unreachable() -> None:
    check, category, _ = classify(
        RefusedResponseError("redirect to an untrusted origin"), default="preview"
    )

    assert (check, category) == ("reachable", "unreachable")
