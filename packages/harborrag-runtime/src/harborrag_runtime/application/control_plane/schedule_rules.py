"""Timetable rules a schedule must meet before it is stored.

Pure functions: no I/O. Every broken rule is reported at once, as ``{field, issue}``
pairs on one ``HarborValidationError``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from harborrag_core.config_store.durations import (
    format_iso8601_duration,
    iso8601_duration_seconds,
    parse_iso8601_duration_seconds,
)
from harborrag_core.contracts.errors import HarborValidationError
from harborrag_runtime.scheduling.errors import ScheduleValidationError
from harborrag_runtime.scheduling.validation import (
    MAX_DURATION_SECONDS,
    min_cron_gap_seconds,
    validate_timezone,
)

from .schedule_models import ScheduleWrite

MAX_JITTER_SECONDS: Final = 3_600
MAX_CATCH_UP_WINDOW_SECONDS: Final = 86_400
MISSED_SLOT_WINDOW_SECONDS: Final = 60


@dataclass(frozen=True, slots=True)
class Timetable:
    """A checked timetable in whole seconds.

    Args:
        interval_seconds: Seconds between slots, or ``None`` for a cron timetable.
        cron: The cron expression, or ``None`` for an interval.
        timezone: IANA time zone the cron expression runs in.
        jitter_seconds: Random delay of up to this long before each slot.
        period_seconds: The interval, or the shortest gap between cron runs.
    """

    interval_seconds: int | None
    cron: str | None
    timezone: str
    jitter_seconds: int
    period_seconds: int


def parse_timetable(write: ScheduleWrite, *, min_interval_seconds: int) -> Timetable:
    """Check a schedule's timetable against the rules and the tenant's minimum.

    Args:
        write: The configuration the caller sent.
        min_interval_seconds: ``schedules.min_interval`` for the tenant.

    Returns:
        The timetable in whole seconds.

    Raises:
        HarborValidationError: One or more rules are broken; ``details["errors"]``
            lists each field and issue.
    """
    issues: list[dict[str, str]] = []
    period = _period_seconds(write, min_interval_seconds, issues)
    timezone = _timezone(write.timezone, issues)
    jitter = _jitter_seconds(write.jitter, period, issues)
    if issues or period is None:
        raise HarborValidationError("the schedule's timetable breaks a rule", {"errors": issues})
    return Timetable(
        interval_seconds=period if write.interval is not None else None,
        cron=write.cron,
        timezone=timezone,
        jitter_seconds=jitter,
        period_seconds=period,
    )


def catch_up_window_seconds(*, catch_up: bool, period_seconds: int) -> int:
    """How late a slot missed during downtime may still fire.

    With ``catch_up`` a slot missed within the last period, 24 h at most, still runs;
    without it only a slot late by under a minute does.
    """
    if not catch_up:
        return MISSED_SLOT_WINDOW_SECONDS
    return max(MISSED_SLOT_WINDOW_SECONDS, min(period_seconds, MAX_CATCH_UP_WINDOW_SECONDS))


def _period_seconds(
    write: ScheduleWrite, min_interval_seconds: int, issues: list[dict[str, str]]
) -> int | None:
    """Return the interval or the shortest cron gap, recording a broken rule instead."""
    if (write.interval is None) == (write.cron is None):
        issues.append({"field": "interval", "issue": "set exactly one of interval and cron"})
        return None
    if write.interval is not None:
        return _interval_seconds(write.interval, min_interval_seconds, issues)
    return _cron_gap_seconds(write.cron or "", min_interval_seconds, issues)


def _interval_seconds(
    value: str, min_interval_seconds: int, issues: list[dict[str, str]]
) -> int | None:
    """Parse the interval and hold it between the tenant's minimum and 366 days."""
    try:
        seconds = parse_iso8601_duration_seconds(value)
    except ValueError:
        issues.append({"field": "interval", "issue": "must be an ISO 8601 duration such as PT1H"})
        return None
    if seconds < min_interval_seconds:
        issues.append({"field": "interval", "issue": _below_minimum(min_interval_seconds)})
        return None
    if seconds > MAX_DURATION_SECONDS:
        issues.append({"field": "interval", "issue": "must be at most P366D"})
        return None
    return seconds


def _cron_gap_seconds(
    expression: str, min_interval_seconds: int, issues: list[dict[str, str]]
) -> int | None:
    """Return the cron expression's shortest gap, refusing one under the minimum."""
    try:
        gap = min_cron_gap_seconds(expression)
    except ScheduleValidationError as error:
        issues.append({"field": "cron", "issue": str(error)})
        return None
    if gap < min_interval_seconds:
        issues.append(
            {
                "field": "cron",
                "issue": f"runs closer together than {_minimum(min_interval_seconds)}",
            }
        )
        return None
    return gap


def _timezone(name: str, issues: list[dict[str, str]]) -> str:
    """Return the time zone, recording an unknown one."""
    try:
        validate_timezone(name)
    except ScheduleValidationError:
        issues.append({"field": "timezone", "issue": "must be an IANA time zone such as UTC"})
    return name


def _jitter_seconds(value: str, period: int | None, issues: list[dict[str, str]]) -> int:
    """Parse the jitter and hold it to half the period and one hour."""
    try:
        seconds = iso8601_duration_seconds(value)
    except ValueError:
        issues.append({"field": "jitter", "issue": "must be an ISO 8601 duration such as PT5M"})
        return 0
    limit = MAX_JITTER_SECONDS if period is None else min(MAX_JITTER_SECONDS, period // 2)
    if seconds > limit:
        issues.append(
            {
                "field": "jitter",
                "issue": f"must be at most {format_iso8601_duration(limit)}, half the "
                "interval and at most PT1H",
            }
        )
    return seconds


def _below_minimum(min_interval_seconds: int) -> str:
    return f"must be at least {_minimum(min_interval_seconds)}"


def _minimum(min_interval_seconds: int) -> str:
    return f"{format_iso8601_duration(min_interval_seconds)}, the schedules.min_interval setting"
