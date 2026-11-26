"""A connection test that hits a rate limit reports it as one, whatever its status."""

from __future__ import annotations

import pytest

from harborrag_adapters.connectors.exceptions import RateLimitError
from harborrag_runtime.ingestion.connection_checks import classify

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("status_code", [403, 429])
def test_classify_a_rate_limit_with_status_reports_rate_limited(status_code: int) -> None:
    check, category, message = classify(
        RateLimitError("You have exceeded a secondary rate limit", status_code=status_code),
        default="permissions",
    )

    assert (check, category) == ("reachable", "rate_limited")
    assert message == f"{status_code} from the source: the source is rate limiting requests"
