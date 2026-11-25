"""verify_credentials maps a plain 403 to an authorization failure and keeps rate limits."""

from __future__ import annotations

import pytest

from harborrag_adapters.connectors.exceptions import (
    AuthorizationError,
    FetchError,
    RateLimitError,
)
from harborrag_adapters.connectors.policies.http import verify_credentials

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def _raise(error: Exception) -> None:
    raise error


def test_verify_credentials_keeps_a_403_rate_limit_retryable() -> None:
    limit = RateLimitError("You have exceeded a secondary rate limit.", status_code=403)

    with pytest.raises(RateLimitError) as raised:
        verify_credentials(lambda: _raise(limit), provider="GitHub")

    assert raised.value is limit
    assert raised.value.retryable is True


def test_verify_credentials_maps_a_plain_403_fetch_error_to_authorization() -> None:
    refusal = FetchError("Resource not accessible by integration", status_code=403)

    with pytest.raises(AuthorizationError, match="^DEMO authorization failed") as raised:
        verify_credentials(lambda: _raise(refusal), provider="DEMO")

    assert raised.value.status_code == 403
