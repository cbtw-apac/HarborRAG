"""Schedule business rules and declarative configuration tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from harborrag_runtime.scheduling import load_schedule_catalog
from harborrag_runtime.scheduling.errors import (
    InvalidCronError,
    InvalidTimezoneError,
    ScheduleValidationError,
)
from harborrag_runtime.scheduling.models import (
    ScheduleDefinition,
    ScheduledWorkflow,
    SourceScheduleTarget,
)
from harborrag_runtime.scheduling.validation import (
    MAX_DURATION_SECONDS,
    validate_backfill,
    validate_cron,
    validate_definition,
    validate_timezone,
)


def test_example_schedule_catalog_loads() -> None:
    path = Path(__file__).resolve().parents[6] / "config" / "schedules.example.yaml"
    catalog = load_schedule_catalog(path)

    assert catalog.prune is False
    assert catalog.schedules[0].schedule_id == "artifact-update"
    assert catalog.schedules[0].target.tenant_id == "DEFAULT"


@pytest.mark.parametrize("expression", ["0 1 * * *", "*/15 * * * *", "0 1 * JAN MON"])
def test_supported_cron_forms_are_accepted(expression: str) -> None:
    validate_cron(expression)


@pytest.mark.parametrize("expression", ["0 1 * *", "60 1 * * *", "0 1 * * FUNDAY", "@DAILY"])
def test_invalid_cron_is_rejected(expression: str) -> None:
    with pytest.raises(InvalidCronError):
        validate_cron(expression)


def test_unknown_timezone_is_rejected() -> None:
    with pytest.raises(InvalidTimezoneError):
        validate_timezone("Not/A_Timezone")


def test_schedule_policy_defaults_and_interval_are_loaded(tmp_path: Path) -> None:
    path = tmp_path / "schedules.yaml"
    path.write_text(
        "version: 1\nschedules:\n"
        "  - id: interval-sync\n"
        "    workflow: source_ingestion\n"
        "    interval_seconds: 3600\n"
        "    source:\n"
        "      connection_id: workspace\n",
        encoding="utf-8",
    )

    definition = load_schedule_catalog(path).schedules[0]

    assert definition.cron is None
    assert definition.interval_seconds == 3600
    assert definition.timezone == "UTC"
    assert definition.overlap.value == "skip"
    assert definition.catchup_window_seconds == 3600
    assert definition.jitter_seconds is None
    assert definition.pause_on_failure is False


def test_duplicate_schedule_ids_have_actionable_error(tmp_path: Path) -> None:
    path = tmp_path / "schedules.yaml"
    entry = (
        "  - id: duplicated\n"
        "    workflow: source_ingestion\n"
        "    cron: '@daily'\n"
        "    source:\n"
        "      connection_id: workspace\n"
    )
    path.write_text(f"version: 1\nschedules:\n{entry}{entry}", encoding="utf-8")

    with pytest.raises(ValueError, match="remove one entry or rename it"):
        load_schedule_catalog(path)


def test_interval_only_schedule_rejects_non_utc_timezone() -> None:
    definition = ScheduleDefinition(
        schedule_id="interval-sync",
        workflow=ScheduledWorkflow.SOURCE_INGESTION,
        cron=None,
        interval_seconds=3600,
        timezone="Asia/Ho_Chi_Minh",
        target=SourceScheduleTarget(tenant_id="DEFAULT", connection_id="workspace"),
    )

    with pytest.raises(ScheduleValidationError, match="use a cron expression"):
        validate_definition(definition)


def test_interval_beyond_the_duration_cap_is_rejected() -> None:
    definition = ScheduleDefinition(
        schedule_id="interval-sync",
        workflow=ScheduledWorkflow.SOURCE_INGESTION,
        cron=None,
        interval_seconds=MAX_DURATION_SECONDS + 1,
        target=SourceScheduleTarget(tenant_id="DEFAULT", connection_id="workspace"),
    )

    with pytest.raises(ScheduleValidationError, match="interval_seconds"):
        validate_definition(definition)


def test_backfill_range_is_capped() -> None:
    now = datetime(2026, 10, 1, tzinfo=UTC)

    validate_backfill(now - timedelta(days=31), now, now=now)
    with pytest.raises(ScheduleValidationError, match="at most 31 days"):
        validate_backfill(now - timedelta(days=32), now, now=now)
