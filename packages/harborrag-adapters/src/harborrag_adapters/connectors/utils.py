from __future__ import annotations

from collections.abc import Iterable
from typing import TypeVar

from harborrag_adapters.connectors.exceptions import DocumentProcessingError


DEFAULT_MAX_NESTED_ITEMS = 1000

T = TypeVar("T")


def validate_non_negative_limit(name: str, value: int | None) -> None:
    """Validate optional size/count limits shared by connector configs."""
    if value is not None and value < 0:
        raise ValueError(f"{name} must be greater than or equal to 0")


def extend_with_limit(
    target: list[T],
    values: Iterable[T],
    *,
    limit: int | None,
    label: str,
    setting_name: str,
) -> None:
    """Append one page of nested API results while enforcing a max count."""
    page = list(values)
    enforce_collection_limit(
        count=len(target) + len(page),
        limit=limit,
        label=label,
        setting_name=setting_name,
    )
    target.extend(page)


def enforce_collection_limit(
    *,
    count: int,
    limit: int | None,
    label: str,
    setting_name: str,
) -> None:
    """Raise when a nested collection would exceed its configured cap."""
    if limit is not None and count > limit:
        raise DocumentProcessingError(
            f"{label} count {count} exceeds {setting_name} {limit}"
        )
