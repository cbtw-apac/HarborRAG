"""White-box unit tests for shared connector-config validation helpers."""
from __future__ import annotations

import pytest

from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_adapters.connectors.utils import (
    enforce_collection_limit,
    extend_with_limit,
    validate_non_negative_limit,
)


pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_validate_non_negative_limit_allows_none_and_non_negative_values():
    validate_non_negative_limit("max_items", None)
    validate_non_negative_limit("max_items", 0)
    validate_non_negative_limit("max_items", 5)


def test_validate_non_negative_limit_rejects_negative_values():
    with pytest.raises(ValueError, match="max_items must be greater than or equal to 0"):
        validate_non_negative_limit("max_items", -1)


def test_enforce_collection_limit_allows_within_bounds():
    enforce_collection_limit(count=5, limit=5, label="items", setting_name="max_items")
    enforce_collection_limit(count=5, limit=None, label="items", setting_name="max_items")


def test_enforce_collection_limit_raises_when_exceeded():
    with pytest.raises(DocumentProcessingError, match="exceeds max_items 5"):
        enforce_collection_limit(
            count=6, limit=5, label="items", setting_name="max_items"
        )


def test_extend_with_limit_appends_and_enforces_cap():
    target = [1, 2]
    extend_with_limit(
        target, [3, 4], limit=10, label="items", setting_name="max_items"
    )
    assert target == [1, 2, 3, 4]

    with pytest.raises(DocumentProcessingError, match="exceeds max_items"):
        extend_with_limit(
            target, [5, 6, 7, 8], limit=5, label="items", setting_name="max_items"
        )
