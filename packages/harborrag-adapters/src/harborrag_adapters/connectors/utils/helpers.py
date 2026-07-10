from __future__ import annotations

from collections.abc import Iterable

from harborrag_adapters.connectors.exceptions import DocumentProcessingError

DEFAULT_MAX_NESTED_ITEMS = 1000


def validate_non_negative_limit(name: str, value: int | None) -> None:
    """Validate optional size/count limits shared by connector configs."""
    if value is not None and value < 0:
        raise ValueError(f"{name} must be greater than or equal to 0")


def validate_http_tuning(
    *,
    requests_per_minute: int,
    request_timeout_seconds: float,
    max_retries: int,
    backoff_factor: float,
) -> None:
    """Validate the shared operational controls used by HTTP connectors."""
    if (
        isinstance(requests_per_minute, bool)
        or not isinstance(requests_per_minute, int)
        or not 1 <= requests_per_minute <= 6000
    ):
        raise ValueError("requests_per_minute must be between 1 and 6000")
    if (
        isinstance(request_timeout_seconds, bool)
        or not isinstance(request_timeout_seconds, (int, float))
        or request_timeout_seconds <= 0
    ):
        raise ValueError("request_timeout_seconds must be greater than 0")
    if (
        isinstance(max_retries, bool)
        or not isinstance(max_retries, int)
        or max_retries < 0
    ):
        raise ValueError("max_retries must be greater than or equal to 0")
    if (
        isinstance(backoff_factor, bool)
        or not isinstance(backoff_factor, (int, float))
        or backoff_factor < 0
    ):
        raise ValueError("backoff_factor must be greater than or equal to 0")


def extend_with_limit[T](
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
