"""A rate limit stays retryable whatever HTTP status the source answered it with."""

from __future__ import annotations

import pytest

from harborrag_adapters.connectors.exceptions import RateLimitError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("status_code", [None, 403, 429])
def test_rate_limit_error_with_any_status_stays_retryable(status_code: int | None) -> None:
    error = RateLimitError("slow down", status_code=status_code)

    assert error.retryable is True
    assert error.status_code == status_code
