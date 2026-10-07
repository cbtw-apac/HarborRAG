"""Provider-neutral schedule definitions owned by HarborRAG, not the Temporal UI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class ScheduleOverlap(StrEnum):
    SKIP = "skip"
    BUFFER_ONE = "buffer_one"
    BUFFER_ALL = "buffer_all"
    CANCEL_OTHER = "cancel_other"
    TERMINATE_OTHER = "terminate_other"
    ALLOW_ALL = "allow_all"


class ScheduleOwner(StrEnum):
    """Who may change a schedule; config-owned schedules are reconciled from Git."""

    API = "api"
    CONFIG = "config"


class ScheduledWorkflow(StrEnum):
    SOURCE_INGESTION = "source_ingestion"


@dataclass(frozen=True, slots=True)
class SourceScheduleTarget:
    tenant_id: str
    connection_id: str
    source_scope_id: str | None = None
    force_reprocess: bool = False


@dataclass(frozen=True, slots=True)
class ScheduleDefinition:
    schedule_id: str
    workflow: ScheduledWorkflow
    cron: str | None
    target: SourceScheduleTarget
    interval_seconds: int | None = None
    timezone: str = "UTC"
    overlap: ScheduleOverlap = ScheduleOverlap.SKIP
    catchup_window_seconds: int = 3600
    jitter_seconds: int | None = None
    pause_on_failure: bool = False
    note: str | None = None
    paused: bool = False
    owner: ScheduleOwner = ScheduleOwner.API

    def to_memo(self) -> dict[str, Any]:
        return {
            "schedule_id": self.schedule_id,
            "workflow": self.workflow.value,
            "cron": self.cron,
            "interval_seconds": self.interval_seconds,
            "timezone": self.timezone,
            "overlap": self.overlap.value,
            "catchup_window_seconds": self.catchup_window_seconds,
            "jitter_seconds": self.jitter_seconds,
            "pause_on_failure": self.pause_on_failure,
            "desired_paused": self.paused,
            "owner": self.owner.value,
            "target": {
                "tenant_id": self.target.tenant_id,
                "connection_id": self.target.connection_id,
                "source_scope_id": self.target.source_scope_id,
                "force_reprocess": self.target.force_reprocess,
            },
        }

    @classmethod
    def from_memo(
        cls,
        memo: dict[str, Any],
        *,
        paused: bool = False,
        note: str | None = None,
    ) -> ScheduleDefinition:
        target = memo["target"]
        return cls(
            schedule_id=str(memo["schedule_id"]),
            workflow=ScheduledWorkflow(memo["workflow"]),
            cron=memo.get("cron"),
            interval_seconds=memo.get("interval_seconds"),
            timezone=str(memo["timezone"]),
            overlap=ScheduleOverlap(memo["overlap"]),
            catchup_window_seconds=int(memo.get("catchup_window_seconds", 3600)),
            jitter_seconds=memo.get("jitter_seconds"),
            pause_on_failure=bool(memo.get("pause_on_failure", False)),
            owner=ScheduleOwner(memo["owner"]),
            target=SourceScheduleTarget(
                tenant_id=str(target["tenant_id"]),
                connection_id=str(target["connection_id"]),
                source_scope_id=target.get("source_scope_id"),
                force_reprocess=bool(target.get("force_reprocess", False)),
            ),
            paused=paused,
            note=note,
        )


@dataclass(frozen=True, slots=True)
class ScheduleRun:
    scheduled_at: datetime
    started_at: datetime
    workflow_id: str | None = None


@dataclass(frozen=True, slots=True)
class ScheduleView:
    """A schedule as Temporal currently runs it."""

    definition: ScheduleDefinition
    next_action_times: tuple[datetime, ...] = ()
    recent_actions: tuple[ScheduleRun, ...] = ()
    num_actions: int | None = None
    num_actions_skipped_overlap: int = 0
    desired_paused: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ScheduleSyncReport:
    created: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
