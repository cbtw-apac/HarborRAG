"""ISO 8601 duration parsing shared by the settings registry and its resolvers."""

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

    seconds = iso8601_duration_seconds(value)
    if seconds <= 0:
        raise ValueError(f"invalid ISO 8601 duration: {value!r}")
    return seconds


def iso8601_duration_seconds(value: str) -> int:
    """Convert a ``PnDTnHnMnS`` duration string to whole seconds, ``PT0S`` included.

    Raises:
        ValueError: ``value`` is not a valid ISO 8601 duration.
    """

    match = _ISO8601_DURATION.match(value.strip())
    if match is None or not any(match.groups()):
        raise ValueError(f"invalid ISO 8601 duration: {value!r}")
    parts = {name: int(digits) for name, digits in match.groupdict().items() if digits}
    return (
        parts.get("days", 0) * 86_400
        + parts.get("hours", 0) * 3_600
        + parts.get("minutes", 0) * 60
        + parts.get("seconds", 0)
    )


def format_iso8601_duration(seconds: int) -> str:
    """Write whole seconds as the shortest ``PnDTnHnMnS`` string, such as ``PT1H``.

    Raises:
        ValueError: ``seconds`` is negative.
    """

    if seconds < 0:
        raise ValueError("a duration cannot be negative")
    days, rest = divmod(seconds, 86_400)
    hours, rest = divmod(rest, 3_600)
    minutes, rest = divmod(rest, 60)
    date_part = f"{days}D" if days else ""
    time_part = "".join(
        f"{amount}{unit}" for amount, unit in ((hours, "H"), (minutes, "M"), (rest, "S")) if amount
    )
    if not date_part and not time_part:
        return "PT0S"
    return f"P{date_part}" + (f"T{time_part}" if time_part else "")
