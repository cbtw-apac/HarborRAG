"""Retry intent carried by connector fetch errors."""

from __future__ import annotations

import pytest

from harborrag_adapters.connectors.exceptions import FetchError, ItemNotFoundError, RateLimitError
from harborrag_adapters.connectors.policies.network import BlockedDestinationError
from harborrag_adapters.connectors.sharepoint.skips import (
    SharePointItemMissingError,
    SharePointItemUnavailableError,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 409, 410, 422])
def test_client_error_status_is_not_retryable(status_code: int) -> None:
    assert FetchError("rejected", status_code=status_code).retryable is False


@pytest.mark.parametrize("status_code", [None, 408, 429, 500, 502, 503, 504])
def test_transient_status_stays_retryable(status_code: int | None) -> None:
    assert FetchError("failed", status_code=status_code).retryable is True


@pytest.mark.parametrize(
    "error",
    [
        BlockedDestinationError("wiki.internal resolves to the private address 10.0.0.5"),
        ItemNotFoundError("gone", status_code=404),
        SharePointItemMissingError("gone", item_id="item-1"),
        SharePointItemUnavailableError("still throttled", status_code=429, item_id="item-1"),
        SharePointItemUnavailableError("still failing", status_code=503, item_id="item-1"),
    ],
)
def test_subclass_that_is_never_retried_stays_non_retryable(error: FetchError) -> None:
    assert error.retryable is False


def test_rate_limit_stays_retryable() -> None:
    assert RateLimitError("slow down", status_code=429).retryable is True
