"""Business rules the Temporal UI cannot enforce on its own."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import (
    InvalidCronError,
    InvalidTimezoneError,
    ScheduleValidationError,
    UnsupportedWorkflowError,
)
from .models import ScheduleDefinition, ScheduledWorkflow, ScheduleOverlap

SCHEDULE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
TENANT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_NOTE_LENGTH = 1000

_CRON_MACROS = frozenset(
    {"@yearly", "@annually", "@monthly", "@weekly", "@daily", "@midnight", "@hourly"}
)
_MONTHS = {
    name: index
    for index, name in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"),
        start=1,
    )
}
_WEEKDAYS = {
    name: index for index, name in enumerate(("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"))
}


@dataclass(frozen=True, slots=True)
class _CronField:
    label: str
    minimum: int
    maximum: int
    names: Mapping[str, int]


_CRON_FIELDS = (
    _CronField("minute", 0, 59, {}),
    _CronField("hour", 0, 23, {}),
    _CronField("day-of-month", 1, 31, {}),
    _CronField("month", 1, 12, _MONTHS),
    _CronField("day-of-week", 0, 6, _WEEKDAYS),
)


@dataclass(frozen=True, slots=True)
class WorkflowPolicy:
    temporal_workflow: str
    allowed_overlaps: frozenset[ScheduleOverlap]


# Only workflows listed here may be scheduled; Temporal appends each action time
# to the base workflow ID so every scheduled firing has a distinct execution ID.
WORKFLOW_POLICIES: Mapping[ScheduledWorkflow, WorkflowPolicy] = {
    ScheduledWorkflow.SOURCE_INGESTION: WorkflowPolicy(
        temporal_workflow="harborrag.scheduled_source_ingestion",
        allowed_overlaps=frozenset(ScheduleOverlap),
    ),
}


def parse_workflow(value: str) -> ScheduledWorkflow:
    try:
        return ScheduledWorkflow(value)
    except ValueError:
        supported = ", ".join(sorted(item.value for item in WORKFLOW_POLICIES))
        raise UnsupportedWorkflowError(
            f"workflow {value!r} cannot be scheduled; supported: {supported}"
        ) from None


def workflow_policy(workflow: ScheduledWorkflow) -> WorkflowPolicy:
    policy = WORKFLOW_POLICIES.get(workflow)
    if policy is None:
        raise UnsupportedWorkflowError(f"workflow {workflow.value!r} cannot be scheduled")
    return policy


def validate_schedule_id(schedule_id: str) -> None:
    if not SCHEDULE_ID_PATTERN.fullmatch(schedule_id):
        raise ScheduleValidationError(
            "schedule_id must be 1-128 characters of letters, digits, '.', '_' or '-' "
            "and start with a letter or digit"
        )


def validate_cron(expression: str) -> None:
    if expression != expression.strip() or not expression:
        raise InvalidCronError("cron must be non-empty and have no outer whitespace")
    if expression.startswith("@"):
        if expression.lower() not in _CRON_MACROS:
            raise InvalidCronError(f"unsupported cron macro {expression!r}")
        return
    parts = expression.split()
    if len(parts) != len(_CRON_FIELDS):
        raise InvalidCronError(
            "cron must have 5 fields (minute hour day-of-month month day-of-week); "
            "set the timezone separately"
        )
    for part, spec in zip(parts, _CRON_FIELDS, strict=True):
        for item in part.split(","):
            _validate_cron_item(item, spec, expression)


def _validate_cron_item(item: str, spec: _CronField, expression: str) -> None:
    base, _, step = item.partition("/")
    if step and (not step.isdigit() or int(step) < 1):
        raise InvalidCronError(f"invalid {spec.label} step in cron {expression!r}")
    if base == "*":
        return
    start, _, end = base.partition("-")
    low = _cron_value(start, spec, expression)
    if end:
        high = _cron_value(end, spec, expression)
        if low > high:
            raise InvalidCronError(f"invalid {spec.label} range in cron {expression!r}")
    elif step:
        raise InvalidCronError(f"{spec.label} step needs '*' or a range in cron {expression!r}")


def _cron_value(token: str, spec: _CronField, expression: str) -> int:
    upper = token.upper()
    if upper in spec.names:
        return spec.names[upper]
    if not token.isdigit() or not spec.minimum <= int(token) <= spec.maximum:
        raise InvalidCronError(
            f"{spec.label} must be {spec.minimum}-{spec.maximum} in cron {expression!r}"
        )
    return int(token)


def validate_timezone(name: str) -> None:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise InvalidTimezoneError(f"unknown IANA timezone {name!r}") from None


def validate_note(note: str | None) -> None:
    if note is not None and len(note) > MAX_NOTE_LENGTH:
        raise ScheduleValidationError(f"note must be at most {MAX_NOTE_LENGTH} characters")


def validate_definition(definition: ScheduleDefinition) -> None:
    validate_schedule_id(definition.schedule_id)
    policy = workflow_policy(definition.workflow)
    if definition.cron is not None:
        validate_cron(definition.cron)
    if definition.interval_seconds is not None and definition.interval_seconds < 1:
        raise ScheduleValidationError("interval_seconds must be at least 1")
    if definition.cron is None and definition.interval_seconds is None:
        raise ScheduleValidationError("provide cron, interval_seconds, or both")
    validate_timezone(definition.timezone)
    if definition.cron is None and definition.timezone != "UTC":
        raise ScheduleValidationError(
            "interval-only schedules are UTC/epoch aligned; use a cron expression "
            "for a local-time schedule"
        )
    validate_note(definition.note)
    if definition.catchup_window_seconds < 10:
        raise ScheduleValidationError("catchup_window_seconds must be at least 10 seconds")
    if definition.jitter_seconds is not None and definition.jitter_seconds < 1:
        raise ScheduleValidationError("jitter_seconds must be at least 1 when provided")
    if definition.overlap not in policy.allowed_overlaps:
        allowed = ", ".join(sorted(item.value for item in policy.allowed_overlaps))
        raise ScheduleValidationError(
            f"overlap {definition.overlap.value!r} is not allowed for "
            f"{definition.workflow.value}; allowed: {allowed}"
        )
    target = definition.target
    if not TENANT_PATTERN.fullmatch(target.tenant_id):
        raise ScheduleValidationError("tenant must be a valid tenant identifier")
    if not target.connection_id.strip() or target.connection_id != target.connection_id.strip():
        raise ScheduleValidationError("connection_id must be non-empty without outer whitespace")


def validate_backfill(start_at: datetime, end_at: datetime, *, now: datetime) -> None:
    if start_at.tzinfo is None or end_at.tzinfo is None:
        raise ScheduleValidationError("backfill start_at and end_at must include a timezone")
    if start_at >= end_at:
        raise ScheduleValidationError("backfill start_at must be before end_at")
    if end_at > now:
        raise ScheduleValidationError("backfill end_at cannot be in the future")


__all__ = [
    "WORKFLOW_POLICIES",
    "WorkflowPolicy",
    "parse_workflow",
    "validate_backfill",
    "validate_cron",
    "validate_definition",
    "validate_note",
    "validate_schedule_id",
    "validate_timezone",
    "workflow_policy",
]
