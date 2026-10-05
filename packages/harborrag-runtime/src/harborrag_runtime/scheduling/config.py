"""Strict loader for version-controlled schedule declarations (config/schedules.yaml)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harborrag_runtime.config.errors import ScheduleConfigurationError
from harborrag_runtime.config.loading import (
    read_yaml_file,
    reject_unknown_keys,
    require_boolean,
    require_integer,
    require_nonblank_string,
    require_optional_nonblank_string,
    require_schema_version,
    require_string_mapping,
)

from .errors import ScheduleValidationError
from .models import (
    ScheduleDefinition,
    ScheduleOverlap,
    ScheduleOwner,
    SourceScheduleTarget,
)
from .validation import parse_workflow, validate_definition

SCHEDULE_CONFIG_VERSION = 1
_ROOT_KEYS = frozenset({"version", "prune", "schedules"})
_SCHEDULE_KEYS = frozenset(
    {
        "id",
        "workflow",
        "cron",
        "interval_seconds",
        "timezone",
        "overlap",
        "catchup_window_seconds",
        "jitter_seconds",
        "pause_on_failure",
        "paused",
        "note",
        "source",
    }
)
_REQUIRED_SCHEDULE_KEYS = frozenset({"id", "workflow", "source"})
_SOURCE_KEYS = frozenset({"tenant", "connection_id", "source_scope_id", "mode"})
_MODES = {"incremental": False, "force": True}


@dataclass(frozen=True, slots=True)
class ScheduleCatalog:
    schedules: tuple[ScheduleDefinition, ...] = ()
    prune: bool = False


def load_schedule_catalog(path: str | Path) -> ScheduleCatalog:
    """Load and validate every declared schedule; any invalid entry fails the whole file."""

    _, raw = read_yaml_file(
        path,
        label="Schedule configuration",
        error_type=ScheduleConfigurationError,
    )
    root = _mapping(raw, "schedule configuration root")
    _reject_unknown(root, _ROOT_KEYS, "schedule configuration root")
    require_schema_version(
        root.get("version"),
        expected=SCHEDULE_CONFIG_VERSION,
        label="Schedule configuration",
        error_type=ScheduleConfigurationError,
    )
    prune = require_boolean(
        root.get("prune", False),
        label="schedule configuration prune",
        error_type=ScheduleConfigurationError,
    )
    entries = root.get("schedules") or []
    if not isinstance(entries, list):
        raise ScheduleConfigurationError("schedules must be a list")
    schedules = tuple(_definition(entry, index) for index, entry in enumerate(entries))
    seen: set[str] = set()
    for schedule in schedules:
        if schedule.schedule_id in seen:
            raise ScheduleConfigurationError(
                f"duplicate schedule id {schedule.schedule_id!r}; remove one entry or rename it"
            )
        seen.add(schedule.schedule_id)
    return ScheduleCatalog(schedules=schedules, prune=prune)


def _definition(raw: object, index: int) -> ScheduleDefinition:
    label = f"schedules[{index}]"
    entry = _mapping(raw, label)
    _reject_unknown(entry, _SCHEDULE_KEYS, label)
    missing = sorted(_REQUIRED_SCHEDULE_KEYS - set(entry))
    if missing:
        raise ScheduleConfigurationError(f"{label} is missing field(s): {', '.join(missing)}")
    source = _mapping(entry["source"], f"{label}.source")
    _reject_unknown(source, _SOURCE_KEYS, f"{label}.source")
    mode = _string(source.get("mode", "incremental"), f"{label}.source.mode")
    if mode not in _MODES:
        raise ScheduleConfigurationError(f"{label}.source.mode must be incremental or force")
    try:
        definition = ScheduleDefinition(
            schedule_id=_string(entry["id"], f"{label}.id"),
            workflow=parse_workflow(_string(entry["workflow"], f"{label}.workflow")),
            cron=(
                _string(entry["cron"], f"{label}.cron")
                if entry.get("cron") is not None
                else None
            ),
            interval_seconds=_optional_integer(entry, "interval_seconds", label),
            timezone=_string(entry.get("timezone", "UTC"), f"{label}.timezone"),
            overlap=_overlap(entry.get("overlap", ScheduleOverlap.SKIP.value), label),
            catchup_window_seconds=require_integer(
                entry.get("catchup_window_seconds", 3600),
                label=f"{label}.catchup_window_seconds",
                error_type=ScheduleConfigurationError,
            ),
            jitter_seconds=_optional_integer(entry, "jitter_seconds", label),
            pause_on_failure=require_boolean(
                entry.get("pause_on_failure", False),
                label=f"{label}.pause_on_failure",
                error_type=ScheduleConfigurationError,
            ),
            paused=require_boolean(
                entry.get("paused", False),
                label=f"{label}.paused",
                error_type=ScheduleConfigurationError,
            ),
            note=require_optional_nonblank_string(
                entry.get("note"),
                label=f"{label}.note",
                error_type=ScheduleConfigurationError,
            ),
            owner=ScheduleOwner.CONFIG,
            target=SourceScheduleTarget(
                tenant_id=_string(source.get("tenant", "DEFAULT"), f"{label}.source.tenant"),
                connection_id=_string(
                    source.get("connection_id"), f"{label}.source.connection_id"
                ),
                source_scope_id=require_optional_nonblank_string(
                    source.get("source_scope_id"),
                    label=f"{label}.source.source_scope_id",
                    error_type=ScheduleConfigurationError,
                ),
                force_reprocess=_MODES[mode],
            ),
        )
        validate_definition(definition)
    except ScheduleValidationError as error:
        raise ScheduleConfigurationError(f"{label}: {error}") from error
    return definition


def _overlap(value: object, label: str) -> ScheduleOverlap:
    text = _string(value, f"{label}.overlap")
    try:
        return ScheduleOverlap(text)
    except ValueError:
        allowed = ", ".join(item.value for item in ScheduleOverlap)
        raise ScheduleConfigurationError(f"{label}.overlap must be one of: {allowed}") from None


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    return require_string_mapping(value, label=label, error_type=ScheduleConfigurationError)


def _reject_unknown(value: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    reject_unknown_keys(value, allowed, label=label, error_type=ScheduleConfigurationError)


def _string(value: object, label: str) -> str:
    return require_nonblank_string(value, label=label, error_type=ScheduleConfigurationError)


def _optional_integer(values: Mapping[str, Any], name: str, label: str) -> int | None:
    value = values.get(name)
    if value is None:
        return None
    return require_integer(
        value,
        label=f"{label}.{name}",
        error_type=ScheduleConfigurationError,
    )


__all__ = ["SCHEDULE_CONFIG_VERSION", "ScheduleCatalog", "load_schedule_catalog"]
