"""Schedule management layer: validation, ownership and reconciliation over a backend."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from harborrag_runtime.ingestion_contracts import PreparedSourceSubmission, SourceSubmission

from .config import ScheduleCatalog
from .errors import (
    DuplicateScheduleError,
    ScheduleConflictError,
    ScheduleNotFoundError,
    ScheduleOwnershipError,
    ScheduleValidationError,
)
from .models import (
    ScheduleDefinition,
    ScheduleOwner,
    ScheduleSyncReport,
    ScheduleView,
    SourceScheduleTarget,
)
from .validation import (
    validate_backfill,
    validate_definition,
    validate_note,
    validate_schedule_id,
)

logger = logging.getLogger("harborrag.runtime.scheduling")

type SourcePreparer = Callable[[SourceSubmission], PreparedSourceSubmission]


class ScheduleBackend(Protocol):
    """Engine that runs schedules; the Temporal implementation uses the Schedules API."""

    async def create(self, definition: ScheduleDefinition) -> None: ...

    async def update(self, definition: ScheduleDefinition, *, desired_paused: bool) -> None: ...

    async def describe(self, schedule_id: str) -> ScheduleView: ...

    async def list_all(self) -> list[ScheduleView]: ...

    async def pause(self, schedule_id: str, *, note: str | None) -> None: ...

    async def unpause(self, schedule_id: str, *, note: str | None) -> None: ...

    async def trigger(self, schedule_id: str) -> None: ...

    async def delete(self, schedule_id: str) -> None: ...

    async def backfill(self, schedule_id: str, *, start_at: datetime, end_at: datetime) -> None: ...


class ScheduleService:
    """Source of truth for schedules; the Temporal UI is only for monitoring."""

    def __init__(
        self,
        backend: ScheduleBackend,
        prepare_source: SourcePreparer,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._backend = backend
        self._prepare_source = prepare_source
        self._clock = clock

    async def create_schedule(self, definition: ScheduleDefinition) -> ScheduleView:
        validate_definition(definition)
        self._validate_source(definition)
        await self._backend.create(definition)
        return await self._backend.describe(definition.schedule_id)

    async def update_schedule(
        self,
        definition: ScheduleDefinition,
        *,
        apply_pause: bool = True,
    ) -> ScheduleView:
        """Replace a schedule's definition.

        With ``apply_pause`` the schedule is paused or unpaused to match
        ``definition.paused`` (recording ``definition.note``); without it the
        live pause state and note are kept, e.g. an operator's incident pause.
        """

        validate_definition(definition)
        current = await self._backend.describe(definition.schedule_id)
        existing = current.definition
        self._require_owner(existing, definition.owner)
        if existing.target.tenant_id != definition.target.tenant_id:
            raise ScheduleValidationError("a schedule's tenant cannot change")
        if existing.workflow is not definition.workflow:
            raise ScheduleValidationError("a schedule's workflow cannot change")
        schedule_id = definition.schedule_id
        self._validate_source(definition)
        await self._backend.update(
            definition,
            desired_paused=definition.paused if apply_pause else current.desired_paused,
        )
        if apply_pause and (
            definition.paused != existing.paused
            or (definition.note is not None and definition.note != existing.note)
        ):
            if definition.paused:
                await self._backend.pause(schedule_id, note=definition.note)
            else:
                await self._backend.unpause(schedule_id, note=definition.note)
        return await self._backend.describe(schedule_id)

    async def pause_schedule(self, schedule_id: str, *, note: str | None = None) -> None:
        validate_note(note)
        await self.describe_schedule(schedule_id)
        await self._backend.pause(schedule_id, note=note)

    async def unpause_schedule(self, schedule_id: str, *, note: str | None = None) -> None:
        validate_note(note)
        await self.describe_schedule(schedule_id)
        await self._backend.unpause(schedule_id, note=note)

    async def trigger_now(self, schedule_id: str) -> None:
        await self.describe_schedule(schedule_id)
        await self._backend.trigger(schedule_id)

    async def delete_schedule(
        self,
        schedule_id: str,
        *,
        owner: ScheduleOwner = ScheduleOwner.API,
    ) -> None:
        existing = await self.describe_schedule(schedule_id)
        self._require_owner(existing.definition, owner)
        await self._backend.delete(schedule_id)

    async def describe_schedule(self, schedule_id: str) -> ScheduleView:
        validate_schedule_id(schedule_id)
        return await self._backend.describe(schedule_id)

    async def list_schedules(
        self,
        *,
        tenant_ids: frozenset[str] | None = None,
    ) -> list[ScheduleView]:
        views = await self._backend.list_all()
        if tenant_ids is not None:
            views = [view for view in views if view.definition.target.tenant_id in tenant_ids]
        return sorted(views, key=lambda view: view.definition.schedule_id)

    async def backfill_schedule(
        self,
        schedule_id: str,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> None:
        validate_backfill(start_at, end_at, now=self._clock())
        await self.describe_schedule(schedule_id)
        await self._backend.backfill(schedule_id, start_at=start_at, end_at=end_at)

    async def sync(self, catalog: ScheduleCatalog) -> ScheduleSyncReport:
        """Reconcile config-owned schedules.

        A declared ``paused`` value is applied on creation and whenever it
        changes in config; otherwise the live pause state (an operator's
        incident pause) is kept. Per-schedule rule violations, rejections and
        ownership conflicts are reported and skipped; backend outages propagate
        so the caller can retry the whole pass.
        """

        created: list[str] = []
        updated: list[str] = []
        deleted: list[str] = []
        failed: list[str] = []
        for definition in catalog.schedules:
            schedule_id = definition.schedule_id
            try:
                if await self._sync_one(definition):
                    created.append(schedule_id)
                    logger.info("Declared schedule created schedule_id=%s", schedule_id)
                else:
                    updated.append(schedule_id)
                    logger.info("Declared schedule updated schedule_id=%s", schedule_id)
            except (ScheduleValidationError, ScheduleConflictError) as error:
                logger.error("Declared schedule %s was not applied: %s", schedule_id, error)
                failed.append(schedule_id)
        if catalog.prune:
            declared = {definition.schedule_id for definition in catalog.schedules}
            for view in await self._backend.list_all():
                schedule_id = view.definition.schedule_id
                if view.definition.owner is ScheduleOwner.CONFIG and schedule_id not in declared:
                    try:
                        await self._backend.delete(schedule_id)
                    except ScheduleNotFoundError:
                        continue
                    deleted.append(schedule_id)
                    logger.info("Undeclared config schedule deleted schedule_id=%s", schedule_id)
        report = ScheduleSyncReport(
            created=tuple(created),
            updated=tuple(updated),
            deleted=tuple(deleted),
            failed=tuple(failed),
        )
        logger.info(
            "Declared schedules synced created=%d updated=%d deleted=%d failed=%d",
            len(report.created),
            len(report.updated),
            len(report.deleted),
            len(report.failed),
        )
        return report

    async def _sync_one(self, definition: ScheduleDefinition) -> bool:
        """Create or update one declared schedule; return whether it was created."""

        validate_definition(definition)
        self._validate_source(definition)
        try:
            await self._backend.create(definition)
            return True
        except DuplicateScheduleError:
            pass
        try:
            current = await self._backend.describe(definition.schedule_id)
        except ScheduleNotFoundError:
            # Exists in Temporal but is not a HarborRAG schedule.
            current = None
        if current is None or current.definition.owner is not ScheduleOwner.CONFIG:
            raise ScheduleOwnershipError(
                f"schedule {definition.schedule_id!r} already exists and is not "
                "managed by configuration; delete it or rename the declared schedule"
            ) from None
        declared_changed = (
            current.declared_paused is not None and current.declared_paused != definition.paused
        )
        await self.update_schedule(definition, apply_pause=declared_changed)
        return False

    def _validate_source(self, definition: ScheduleDefinition) -> None:
        """Check the target resolves now; each firing prepares its own input."""

        target: SourceScheduleTarget = definition.target
        try:
            self._prepare_source(
                SourceSubmission(
                    # Placeholder; each scheduled run derives its own task ID.
                    task_id=definition.schedule_id,
                    tenant_id=target.tenant_id,
                    connector_name=target.connection_id,
                    connection_id=target.connection_id,
                    source_scope_id=target.source_scope_id,
                    force_reprocess=target.force_reprocess,
                )
            )
        except ValueError as error:
            raise ScheduleValidationError(str(error)) from error

    @staticmethod
    def _require_owner(existing: ScheduleDefinition, owner: ScheduleOwner) -> None:
        if existing.owner is ScheduleOwner.CONFIG and owner is not ScheduleOwner.CONFIG:
            raise ScheduleOwnershipError(
                f"schedule {existing.schedule_id!r} is managed by version-controlled "
                "configuration; change config/schedules.yaml instead"
            )
