"""Timetable rules of ``/v1/admin/schedules`` and the cron gap they rely on."""

from __future__ import annotations

import pytest

from harborrag_core.contracts.errors import HarborValidationError
from harborrag_runtime.application.control_plane.schedule_models import ScheduleWrite
from harborrag_runtime.application.control_plane.schedule_rules import (
    Timetable,
    catch_up_window_seconds,
    parse_timetable,
)
from harborrag_runtime.scheduling.errors import ScheduleValidationError
from harborrag_runtime.scheduling.validation import min_cron_gap_seconds

pytestmark = pytest.mark.unit

FIFTEEN_MINUTES = 900


def _write(**changes: object) -> ScheduleWrite:
    values: dict[str, object] = {"project_id": "demo", "connection_id": "demo-tickets"}
    values.update(changes)
    return ScheduleWrite(**values)


def test_parse_timetable_reads_an_interval_with_jitter() -> None:
    timetable = parse_timetable(
        _write(interval="PT1H", jitter="PT5M"), min_interval_seconds=FIFTEEN_MINUTES
    )

    assert timetable == Timetable(
        interval_seconds=3600, cron=None, timezone="UTC", jitter_seconds=300, period_seconds=3600
    )


def test_parse_timetable_reads_a_cron_with_its_shortest_gap() -> None:
    timetable = parse_timetable(
        _write(cron="0 2,8 * * 1-5", timezone="Asia/Ho_Chi_Minh"),
        min_interval_seconds=FIFTEEN_MINUTES,
    )

    assert timetable.interval_seconds is None and timetable.cron == "0 2,8 * * 1-5"
    assert timetable.timezone == "Asia/Ho_Chi_Minh"
    assert timetable.period_seconds == 6 * 3600


def test_parse_timetable_accepts_jitter_of_exactly_half_the_interval() -> None:
    timetable = parse_timetable(
        _write(interval="PT30M", jitter="PT15M"), min_interval_seconds=FIFTEEN_MINUTES
    )

    assert timetable.jitter_seconds == 900


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"interval": "one hour"}, "interval"),
        ({"interval": "P400D"}, "interval"),
        ({"cron": "not a cron"}, "cron"),
        ({"interval": "PT1H", "jitter": "soon"}, "jitter"),
    ],
)
def test_parse_timetable_names_the_field_of_a_malformed_value(
    changes: dict[str, str], field: str
) -> None:
    with pytest.raises(HarborValidationError) as raised:
        parse_timetable(_write(**changes), min_interval_seconds=FIFTEEN_MINUTES)

    assert [error["field"] for error in raised.value.details["errors"]] == [field]


@pytest.mark.parametrize(
    ("catch_up", "period_seconds", "expected"),
    [
        (False, 86_400, 60),
        (True, 3_600, 3_600),
        (True, 30, 60),
        (True, 7 * 86_400, 86_400),
    ],
)
def test_catch_up_window_is_a_minute_or_one_period_up_to_a_day(
    catch_up: bool, period_seconds: int, expected: int
) -> None:
    assert catch_up_window_seconds(catch_up=catch_up, period_seconds=period_seconds) == expected


@pytest.mark.parametrize(
    ("expression", "gap_seconds"),
    [
        ("*/5 * * * *", 300),
        ("0 * * * *", 3_600),
        ("0 2,8 * * *", 6 * 3_600),
        ("30 23 * * *", 86_400),
        ("@hourly", 3_600),
    ],
)
def test_min_cron_gap_is_the_shortest_time_between_runs(expression: str, gap_seconds: int) -> None:
    assert min_cron_gap_seconds(expression) == gap_seconds


def test_min_cron_gap_refuses_an_invalid_expression() -> None:
    with pytest.raises(ScheduleValidationError):
        min_cron_gap_seconds("61 * * * *")
