"""Application service exposing HarborRAG-owned schedules to transports."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from functools import partial

from harborrag_core.contracts.errors import HarborConnectionError
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.errors import (
    RuntimeConnectionError,
    WorkflowOperationError,
    WorkflowSubmissionError,
)
from harborrag_runtime.scheduling import (
    DuplicateScheduleError,
    ScheduleBackend,
    ScheduleDefinition,
    ScheduleNotFoundError,
    ScheduleOverlap,
    ScheduleOwner,
    ScheduleOwnershipError,
    ScheduleService,
    ScheduleValidationError,
    ScheduleView,
    SourceScheduleTarget,
    load_schedule_catalog,
)
from harborrag_runtime.scheduling.validation import parse_workflow

from ..errors import (
    ScheduleAlreadyExistsError,
    ScheduleInvalidError,
    ScheduleManagedByConfigError,
    ScheduleNotFoundAppError,
)
from ..ingestion.ports import SourceInputBuilder
from .models import ScheduleCommand

logger = logging.getLogger("harborrag.app.workflow_control.scheduling")

type ScheduleBackendProvider = Callable[[], Awaitable[ScheduleBackend]]


class ScheduleApplicationService:
    """Translate schedule use cases and runtime failures into public errors."""

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

    async def create(self, command: ScheduleCommand) -> dict[str, object]:
        with _translated():
            definition = _definition(command)
            view = await (await self._service()).create_schedule(definition)
        logger.info(
            "Schedule created schedule_id=%s tenant=%s connection_id=%s cron=%r timezone=%s",
            definition.schedule_id,
            definition.target.tenant_id,
            definition.target.connection_id,
            definition.cron,
            definition.timezone,
        )
        return schedule_response(view)

    async def update(self, command: ScheduleCommand) -> dict[str, object]:
        with _translated():
            service = await self._service()
            definition = _definition(command)
            if command.paused is None:
                # Keep the recorded desired pause state the alerting compares against.
                current = await service.describe_schedule(command.schedule_id)
                definition = replace(definition, paused=current.desired_paused)
            view = await service.update_schedule(definition, apply_pause=command.paused is not None)
        logger.info("Schedule updated schedule_id=%s", command.schedule_id)
        return schedule_response(view)

    async def get(self, schedule_id: str) -> dict[str, object]:
        with _translated():
            return schedule_response(await (await self._service()).describe_schedule(schedule_id))

    async def list_all(self, *, tenant_ids: frozenset[str] | None) -> dict[str, object]:
        with _translated():
            views = await (await self._service()).list_schedules(tenant_ids=tenant_ids)
        return {"items": [schedule_response(view) for view in views]}

    async def pause(self, schedule_id: str, *, note: str | None) -> dict[str, object]:
        with _translated():
            await (await self._service()).pause_schedule(schedule_id, note=note)
        logger.info("Schedule paused schedule_id=%s", schedule_id)
        return _action(schedule_id, "Schedule paused")

    async def unpause(self, schedule_id: str, *, note: str | None) -> dict[str, object]:
        with _translated():
            await (await self._service()).unpause_schedule(schedule_id, note=note)
        logger.info("Schedule unpaused schedule_id=%s", schedule_id)
        return _action(schedule_id, "Schedule unpaused")

    async def trigger(self, schedule_id: str) -> dict[str, object]:
        with _translated():
            await (await self._service()).trigger_now(schedule_id)
        logger.info("Schedule triggered schedule_id=%s", schedule_id)
        return _action(schedule_id, "Schedule run requested")

    async def backfill(
        self,
        schedule_id: str,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> dict[str, object]:
        with _translated():
            await (await self._service()).backfill_schedule(
                schedule_id, start_at=start_at, end_at=end_at
            )
        logger.info(
            "Schedule backfill requested schedule_id=%s start_at=%s end_at=%s",
            schedule_id,
            start_at.isoformat(),
            end_at.isoformat(),
        )
        return _action(schedule_id, "Schedule backfill requested")

    async def delete(self, schedule_id: str) -> dict[str, object]:
        with _translated():
            await (await self._service()).delete_schedule(schedule_id)
        logger.warning("Schedule deleted schedule_id=%s", schedule_id)
        return _action(schedule_id, "Schedule deleted")

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


def _definition(command: ScheduleCommand) -> ScheduleDefinition:
    try:
        overlap = ScheduleOverlap(command.overlap)
    except ValueError:
        raise ScheduleValidationError(f"unsupported overlap {command.overlap!r}") from None
    return ScheduleDefinition(
        schedule_id=command.schedule_id,
        workflow=parse_workflow(command.workflow),
        cron=command.cron,
        interval_seconds=command.interval_seconds,
        timezone=command.timezone,
        overlap=overlap,
        catchup_window_seconds=command.catchup_window_seconds,
        jitter_seconds=command.jitter_seconds,
        pause_on_failure=command.pause_on_failure,
        paused=bool(command.paused),
        note=command.note,
        owner=ScheduleOwner.API,
        target=SourceScheduleTarget(
            tenant_id=command.tenant_id,
            connection_id=command.connection_id,
            source_scope_id=command.source_scope_id,
            force_reprocess=command.force_reprocess,
        ),
    )


@contextmanager
def _translated() -> Iterator[None]:
    try:
        yield
    except ScheduleNotFoundError as error:
        raise ScheduleNotFoundAppError("Schedule was not found.") from error
    except DuplicateScheduleError as error:
        raise ScheduleAlreadyExistsError(str(error)) from error
    except ScheduleOwnershipError as error:
        raise ScheduleManagedByConfigError(str(error)) from error
    except ScheduleValidationError as error:
        raise ScheduleInvalidError(str(error)) from error
    except (RuntimeConnectionError, WorkflowOperationError, WorkflowSubmissionError) as error:
        logger.error("Schedule engine call failed", exc_info=error)
        raise HarborConnectionError("Schedule service is temporarily unavailable.") from error


def _action(schedule_id: str, message: str) -> dict[str, object]:
    return {"schedule_id": schedule_id, "message": message}


def schedule_response(view: ScheduleView) -> dict[str, object]:
    definition = view.definition
    target = definition.target
    return {
        "schedule_id": definition.schedule_id,
        "workflow": definition.workflow.value,
        "cron": definition.cron,
        "interval_seconds": definition.interval_seconds,
        "timezone": definition.timezone,
        "overlap": definition.overlap.value,
        "catchup_window_seconds": definition.catchup_window_seconds,
        "jitter_seconds": definition.jitter_seconds,
        "pause_on_failure": definition.pause_on_failure,
        "paused": definition.paused,
        "note": definition.note,
        "managed_by": definition.owner.value,
        "source": {
            "tenant": target.tenant_id,
            "connection_id": target.connection_id,
            "source_scope_id": target.source_scope_id,
            "mode": "force" if target.force_reprocess else "incremental",
        },
        "next_run_times": list(view.next_action_times),
        "recent_runs": [
            {"scheduled_at": run.scheduled_at, "started_at": run.started_at}
            for run in view.recent_actions
        ],
        "total_runs": view.num_actions,
        "skipped_overlap_runs": view.num_actions_skipped_overlap,
        "created_at": view.created_at,
        "updated_at": view.updated_at,
    }


__all__ = ["ScheduleApplicationService", "ScheduleBackendProvider", "schedule_response"]
