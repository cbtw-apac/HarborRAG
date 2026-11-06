"""Reconcile the declared schedule file into the schedule engine."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from functools import partial

from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.scheduling import ScheduleBackend, ScheduleService, load_schedule_catalog

from ..ingestion.ports import SourceInputBuilder

logger = logging.getLogger("harborrag.app.workflow_control.scheduling")

type ScheduleBackendProvider = Callable[[], Awaitable[ScheduleBackend]]


class ScheduleApplicationService:
    """Apply ``config/schedules.yaml`` to the schedule engine at startup."""

    def __init__(
        self,
        settings: RuntimeSettings,
        *,
        backend_provider: ScheduleBackendProvider,
        source_input_builder: SourceInputBuilder,
    ) -> None:
        self._settings = settings
        self._backend_provider = backend_provider
        self._source_input_builder = source_input_builder

    async def sync_declared(self) -> dict[str, int] | None:
        """Reconcile config/schedules.yaml into the engine; None when no file is deployed."""

        path = self._settings.schedule_config_path
        if not path.is_file():
            logger.info("No declarative schedule file at %s; skipping schedule sync", path)
            return None
        catalog = load_schedule_catalog(path)
        report = await (await self._service()).sync(catalog)
        return {
            "created": len(report.created),
            "updated": len(report.updated),
            "deleted": len(report.deleted),
            "failed": len(report.failed),
        }

    async def _service(self) -> ScheduleService:
        return ScheduleService(
            await self._backend_provider(),
            partial(self._source_input_builder, self._settings),
        )


__all__ = ["ScheduleApplicationService", "ScheduleBackendProvider"]
