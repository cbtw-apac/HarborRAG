from __future__ import annotations

import itertools
import math
from collections.abc import Iterable
from urllib.parse import urlparse

from harborrag_adapters.connectors.exceptions import DocumentProcessingError

DEFAULT_MAX_NESTED_ITEMS = 1000


def validate_non_negative_limit(name: str, value: int | None) -> None:
    """Validate optional size/count limits shared by connector configs."""
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be an integer greater than or equal to 0")


def validate_https_url(name: str, value: str) -> None:
    """Require an HTTPS origin for connector URLs that carry credentials."""
    parsed = urlparse(value)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(f"{name} must be an HTTPS URL without embedded credentials")


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
        or not math.isfinite(request_timeout_seconds)
        or request_timeout_seconds <= 0
    ):
        raise ValueError("request_timeout_seconds must be a finite number greater than 0")
    if (
        isinstance(max_retries, bool)
        or not isinstance(max_retries, int)
        or max_retries < 0
    ):
        raise ValueError("max_retries must be greater than or equal to 0")
    if (
        isinstance(backoff_factor, bool)
        or not isinstance(backoff_factor, (int, float))
        or not math.isfinite(backoff_factor)
        or backoff_factor < 0
    ):
        raise ValueError(
            "backoff_factor must be a finite number greater than or equal to 0"
        )


def extend_with_limit[T](
    target: list[T],
    values: Iterable[T],
    *,
    limit: int | None,
    label: str,
    setting_name: str,
) -> None:
    """Append one page of nested API results while enforcing a max count."""
    if limit is None:
        target.extend(values)
        return
    # Consume at most one item past the remaining allowance so a hostile or
    # accidental unbounded page can't be materialized in full before the
    # limit check runs.
    remaining = max(limit - len(target), 0)
    page = list(itertools.islice(values, remaining + 1))
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
