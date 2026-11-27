"""How the application service composes the ``/v1/admin/schedules`` timers and slot worker."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from harborrag_core.contracts.errors import HarborUnavailableError
from harborrag_runtime.application.composition.factories import AppServiceFactories
from harborrag_runtime.application.composition.resources import AppResources
from harborrag_runtime.application.composition.service import AppService
from harborrag_runtime.application.control_plane.schedules import ConnectionScheduleService
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.errors import RuntimeConnectionError
from harborrag_runtime.temporal import connection as connection_module

from ..application_fixtures import FakeComposition

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_schedule_timers_connect_once_and_retry_after_a_failed_connect() -> None:
    timers = object()
    attempts: list[RuntimeSettings] = []

    async def connect(settings: RuntimeSettings) -> object:
        attempts.append(settings)
        if len(attempts) == 1:
            raise HarborUnavailableError("could not connect to Temporal")
        return timers

    resources = AppResources(
        RuntimeSettings(), factories=AppServiceFactories(schedule_timers=connect)
    )

    with pytest.raises(HarborUnavailableError):
        await resources.schedule_timers()

    assert await resources.schedule_timers() is timers
    assert await resources.schedule_timers() is timers
    assert len(attempts) == 2


async def test_run_schedule_slots_raises_when_temporal_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        connection_module.Client, "connect", AsyncMock(side_effect=RuntimeError("refused"))
    )
    service = AppService(FakeComposition({"runtime": {"ready": True}}))

    assert isinstance(service.schedule_service, ConnectionScheduleService)
    with pytest.raises(RuntimeConnectionError):
        await service.run_schedule_slots()
