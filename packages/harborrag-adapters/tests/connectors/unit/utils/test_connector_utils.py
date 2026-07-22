"""White-box unit tests for shared connector-config validation helpers."""

from __future__ import annotations

import itertools

import pytest

from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_adapters.connectors.utils.helpers import (
    enforce_collection_limit,
    extend_with_limit,
    validate_http_tuning,
    validate_https_url,
    validate_non_negative_limit,
)

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_validate_non_negative_limit_allows_none_and_non_negative_values():
    validate_non_negative_limit("max_items", None)
    validate_non_negative_limit("max_items", 0)
    validate_non_negative_limit("max_items", 5)


def test_validate_non_negative_limit_rejects_negative_values():
    with pytest.raises(ValueError, match="max_items must be an integer greater than or equal to 0"):
        validate_non_negative_limit("max_items", -1)


def test_validate_non_negative_limit_rejects_booleans():
    with pytest.raises(ValueError, match="max_items"):
        validate_non_negative_limit("max_items", True)


def test_validate_https_url_allows_plain_https_origin():
    validate_https_url("base_url", "https://example.com/wiki")


@pytest.mark.parametrize(
    "value",
    [
        "http://example.com",
        "https://",
        "https://user:pass@example.com",
        "https://user@example.com",
        "ftp://example.com",
    ],
)
def test_validate_https_url_rejects_unsafe_urls(value):
    with pytest.raises(ValueError, match="base_url"):
        validate_https_url("base_url", value)


def test_validate_http_tuning_allows_safe_boundary_values():
    validate_http_tuning(
        requests_per_minute=1,
        request_timeout_seconds=0.1,
        max_retries=0,
        backoff_factor=0,
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"requests_per_minute": 0}, "requests_per_minute"),
        ({"request_timeout_seconds": 0}, "request_timeout_seconds"),
        ({"max_retries": -1}, "max_retries"),
        ({"backoff_factor": -0.1}, "backoff_factor"),
        ({"max_retries": True}, "max_retries"),
        ({"request_timeout_seconds": float("nan")}, "request_timeout_seconds"),
        ({"request_timeout_seconds": float("inf")}, "request_timeout_seconds"),
        ({"backoff_factor": float("nan")}, "backoff_factor"),
        ({"backoff_factor": float("inf")}, "backoff_factor"),
    ],
)
def test_validate_http_tuning_rejects_invalid_values(overrides, message):
    values = {
        "requests_per_minute": 60,
        "request_timeout_seconds": 30.0,
        "max_retries": 3,
        "backoff_factor": 0.5,
    }
    values.update(overrides)

    with pytest.raises(ValueError, match=message):
        validate_http_tuning(**values)


def test_enforce_collection_limit_allows_within_bounds():
    enforce_collection_limit(count=5, limit=5, label="items", setting_name="max_items")
    enforce_collection_limit(count=5, limit=None, label="items", setting_name="max_items")


def test_enforce_collection_limit_raises_when_exceeded():
    with pytest.raises(DocumentProcessingError, match="exceeds max_items 5"):
        enforce_collection_limit(count=6, limit=5, label="items", setting_name="max_items")


def test_extend_with_limit_appends_and_enforces_cap():
    target = [1, 2]
    extend_with_limit(target, [3, 4], limit=10, label="items", setting_name="max_items")
    assert target == [1, 2, 3, 4]

    with pytest.raises(DocumentProcessingError, match="exceeds max_items"):
        extend_with_limit(target, [5, 6, 7, 8], limit=5, label="items", setting_name="max_items")


def test_extend_with_limit_does_not_materialize_unbounded_iterables():
    consumed: list[int] = []

    def unbounded():
        for value in itertools.count():
            consumed.append(value)
            yield value

    target: list[int] = []
    with pytest.raises(DocumentProcessingError, match="exceeds max_items 3"):
        extend_with_limit(target, unbounded(), limit=3, label="items", setting_name="max_items")

    # Only the allowance plus one probe item should ever have been pulled.
    assert len(consumed) == 4
