"""Dependency protocol for schedule routes."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Protocol, cast

from fastapi import Depends, Request

from harborrag_app.workflow_control.scheduling.models import ScheduleCommand


class ScheduleService(Protocol):
    async def create_schedule(self, command: ScheduleCommand) -> dict[str, object]: ...

    async def update_schedule(self, command: ScheduleCommand) -> dict[str, object]: ...

    async def list_schedules(
        self, *, tenant_ids: frozenset[str] | None
    ) -> dict[str, object]: ...

    async def get_schedule(self, schedule_id: str) -> dict[str, object]: ...

    async def pause_schedule(self, schedule_id: str, *, note: str | None) -> dict[str, object]: ...

    async def unpause_schedule(self, schedule_id: str, *, note: str | None) -> dict[str, object]: ...

    async def trigger_schedule(self, schedule_id: str) -> dict[str, object]: ...

    async def backfill_schedule(
        self, schedule_id: str, *, start_at: datetime, end_at: datetime
    ) -> dict[str, object]: ...

    async def delete_schedule(self, schedule_id: str) -> dict[str, object]: ...


def schedule_service(request: Request) -> ScheduleService:
    return cast(ScheduleService, request.app.state.app_service)


ScheduleServiceDependency = Annotated[ScheduleService, Depends(schedule_service)]
