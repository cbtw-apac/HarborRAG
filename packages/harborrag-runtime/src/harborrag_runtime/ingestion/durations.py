"""ISO 8601 duration parsing for setting values like ``ingestion.activity_timeout``."""

from __future__ import annotations

import re

_ISO8601_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def parse_iso8601_duration_seconds(value: str) -> int:
    """Convert a ``PnDTnHnMnS`` duration string to whole seconds.

    Raises:
        ValueError: ``value`` is not a valid, non-zero ISO 8601 duration.
    """

    match = _ISO8601_DURATION.match(value.strip())
    if match is None or not any(match.groups()):
        raise ValueError(f"invalid ISO 8601 duration: {value!r}")
    parts = {name: int(digits) for name, digits in match.groupdict().items() if digits}
    seconds = (
        parts.get("days", 0) * 86_400
        + parts.get("hours", 0) * 3_600
        + parts.get("minutes", 0) * 60
        + parts.get("seconds", 0)
    )
    if seconds <= 0:
        raise ValueError(f"invalid ISO 8601 duration: {value!r}")
    return seconds
