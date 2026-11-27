"""Schedule management layer: validation, ownership and reconciliation over a backend."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol

from harborrag_core.contracts.errors import HarborConflictError, HarborValidationError
from harborrag_core.ingestion.run_contracts import PreparedSourceSubmission, SourceSubmission
from harborrag_core.observability.log_events import log_event

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
_REJECTED_SCHEDULE_HINT = (
    "Check that its connection_id exists in its tenant, that its tenant and workflow are "
    "unchanged since it was created, and that its cron or interval_seconds is valid."
)

type SourcePreparer = Callable[[SourceSubmission], Awaitable[PreparedSourceSubmission]]


class ScheduleBackend(Protocol):
    """Engine that runs schedules; the Temporal implementation uses the Schedules API."""

    async def create(self, definition: ScheduleDefinition) -> None:
        """Create the schedule."""
        ...

    async def update(self, definition: ScheduleDefinition, *, desired_paused: bool) -> None:
        """Update the schedule and its paused state."""
        ...

    async def describe(self, schedule_id: str) -> ScheduleView:
        """Return the schedule."""
        ...

    async def list_all(self) -> list[ScheduleView]:
        """Return every schedule."""
        ...

    async def pause(self, schedule_id: str, *, note: str | None) -> None:
        """Pause the schedule."""
        ...

    async def unpause(self, schedule_id: str, *, note: str | None) -> None:
        """Resume the schedule."""
        ...

    async def trigger(self, schedule_id: str) -> None:
        """Run the schedule once now."""
        ...

    async def delete(self, schedule_id: str) -> None:
        """Delete the schedule."""
        ...

    async def backfill(self, schedule_id: str, *, start_at: datetime, end_at: datetime) -> None:
        """Run the schedule for the missed window."""
        ...


class ScheduleService:
    """Source of truth for schedules; the Temporal UI is only for monitoring.

    Args:
        backend: Engine that runs the schedules.
        prepare_source: Resolves a schedule's target the way a run would.
        clock: Current UTC time, replaceable in tests.
    """

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
        """Validate and create a schedule."""
        validate_definition(definition)
        await self._validate_source(definition)
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
        await self._validate_source(definition)
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
        """Pause an existing schedule."""
        validate_note(note)
        await self.describe_schedule(schedule_id)
        await self._backend.pause(schedule_id, note=note)

    async def unpause_schedule(self, schedule_id: str, *, note: str | None = None) -> None:
        """Resume an existing schedule."""
        validate_note(note)
        await self.describe_schedule(schedule_id)
        await self._backend.unpause(schedule_id, note=note)

    async def trigger_now(self, schedule_id: str) -> None:
        """Run an existing schedule once now."""
        await self.describe_schedule(schedule_id)
        await self._backend.trigger(schedule_id)

    async def delete_schedule(
        self,
        schedule_id: str,
        *,
        owner: ScheduleOwner = ScheduleOwner.API,
    ) -> None:
        """Delete an existing schedule."""
        existing = await self.describe_schedule(schedule_id)
        self._require_owner(existing.definition, owner)
        await self._backend.delete(schedule_id)

    async def describe_schedule(self, schedule_id: str) -> ScheduleView:
        """Return a schedule after validating its ID."""
        validate_schedule_id(schedule_id)
        return await self._backend.describe(schedule_id)

    async def list_schedules(
        self,
        *,
        tenant_ids: frozenset[str] | None = None,
    ) -> list[ScheduleView]:
        """List schedules, limited to the tenants given."""
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
        """Run an existing schedule for the missed window."""
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
                    log_event(
                        logger, "scheduling.declared_schedule_created", schedule_id=schedule_id
                    )
                else:
                    updated.append(schedule_id)
                    log_event(
                        logger, "scheduling.declared_schedule_updated", schedule_id=schedule_id
                    )
            except (ScheduleValidationError, ScheduleConflictError) as error:
                log_event(
                    logger,
                    "scheduling.declared_schedule_rejected",
                    level=logging.ERROR,
                    error=error,
                    schedule_id=schedule_id,
                    hint=_REJECTED_SCHEDULE_HINT,
                )
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
                    log_event(
                        logger, "scheduling.undeclared_schedule_deleted", schedule_id=schedule_id
                    )
        report = ScheduleSyncReport(
            created=tuple(created),
            updated=tuple(updated),
            deleted=tuple(deleted),
            failed=tuple(failed),
        )
        log_event(
            logger,
            "scheduling.declared_schedules_synced",
            created_count=len(report.created),
            updated_count=len(report.updated),
            deleted_count=len(report.deleted),
            failed_count=len(report.failed),
        )
        return report

    async def _sync_one(self, definition: ScheduleDefinition) -> bool:
        """Create or update one declared schedule and return whether it was created.

        Args:
            definition: The schedule as configuration declares it.

        Returns:
            True when the schedule was created, False when it was updated.

        Raises:
            ScheduleOwnershipError: A schedule with this ID exists but configuration
                does not own it.
        """
        validate_definition(definition)
        await self._validate_source(definition)
        try:
            await self._backend.create(definition)
            return True
        except DuplicateScheduleError:
            pass
        try:
            current = await self._backend.describe(definition.schedule_id)
        except ScheduleNotFoundError:
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

    async def _validate_source(self, definition: ScheduleDefinition) -> None:
        """Check the target resolves now; each firing prepares its own input.

        The schedule ID stands in for the task ID, because each scheduled run
        derives its own.
        """
        target: SourceScheduleTarget = definition.target
        try:
            await self._prepare_source(
                SourceSubmission(
                    task_id=definition.schedule_id,
                    tenant_id=target.tenant_id,
                    connector_name=target.connection_id,
                    connection_id=target.connection_id,
                    source_scope_id=target.source_scope_id,
                    force_reprocess=target.force_reprocess,
                )
            )
        except (ValueError, HarborValidationError, HarborConflictError) as error:
            raise ScheduleValidationError(str(error)) from error

    @staticmethod
    def _require_owner(existing: ScheduleDefinition, owner: ScheduleOwner) -> None:
        """Reject a change to a configuration-owned schedule from any other owner."""
        if existing.owner is ScheduleOwner.CONFIG and owner is not ScheduleOwner.CONFIG:
            raise ScheduleOwnershipError(
                f"schedule {existing.schedule_id!r} is managed by version-controlled "
                "configuration; change config/sources/schedules.yaml instead"
            )
