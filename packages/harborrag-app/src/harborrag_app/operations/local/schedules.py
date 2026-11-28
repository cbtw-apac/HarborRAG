"""In-process schedule operations.

Each operation checks the caller's role, then makes one schedule-service call. The
transport sets status codes and headers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from harborrag_app.operations.contract import OPERATIONS
from harborrag_app.operations.etags import Preconditions, etag, version_from_etag
from harborrag_app.operations.results import Accepted, Saved, Tagged
from harborrag_app.operations.schemas.connections import IngestionOut
from harborrag_app.operations.schemas.schedules import (
    ScheduleListQuery,
    ScheduleOut,
    SchedulePage,
    SchedulePauseRequest,
    ScheduleWriteRequest,
)
from harborrag_app.operations.scope import ConnectionRef, Scope
from harborrag_core.contracts.errors import HarborValidationError
from harborrag_core.domain.schedule import ScheduleFilter
from harborrag_runtime.application.control_plane.schedule_models import (
    ScheduleKey,
    ScheduleQuery,
    ScheduleWrite,
    WriteConditions,
)

if TYPE_CHECKING:
    from harborrag_runtime.application.composition.service import AppService


class ScheduleOps:
    """The schedule operations of ``LocalOperations``.

    ``LocalOperations`` sets ``_app_service``, whose schedule service runs them.
    """

    _app_service: AppService

    async def list_schedules(self, scope: Scope, query: ScheduleListQuery) -> SchedulePage:
        """List the tenant's schedules, newest first, each with its latest slot.

        Args:
            scope: Caller and tenant.
            query: Filters, ``cursor`` and page size.

        Returns:
            One page and the cursor of the next, ``None`` on the last page.

        Raises:
            HarborValidationError: ``connection_ref`` is malformed or names another project
                than ``project_id``, or ``cursor`` is malformed.
        """
        scope.require(OPERATIONS["list_schedules"].role)
        page = await self._app_service.schedule_service.page(
            scope.tenant_id,
            ScheduleQuery(filters=_schedule_filter(query), limit=query.limit, cursor=query.cursor),
        )
        return SchedulePage.model_validate(page)

    async def get_schedule(self, scope: Scope, schedule_id: str) -> Tagged[ScheduleOut]:
        """Read one schedule with its last 20 slots.

        Args:
            scope: Caller and tenant.
            schedule_id: The schedule to read.

        Returns:
            The schedule and its ETag.

        Raises:
            ScheduleNotFoundError: No such schedule in the tenant.
        """
        scope.require(OPERATIONS["get_schedule"].role)
        view, version = await self._app_service.schedule_service.get(
            ScheduleKey(scope.tenant_id, schedule_id)
        )
        return Tagged(ScheduleOut.model_validate(view), etag(version))

    async def put_schedule(
        self,
        scope: Scope,
        schedule_id: str,
        body: ScheduleWriteRequest,
        *,
        preconditions: Preconditions,
    ) -> Saved[ScheduleOut]:
        """Create or overwrite a schedule; an overwrite keeps its pause and slots.

        Args:
            scope: Caller and tenant; the caller is recorded as the author.
            schedule_id: ID of the schedule, chosen by the caller.
            body: Its operation, input and timetable.
            preconditions: ``If-Match`` and ``If-None-Match`` of the write.

        Returns:
            The stored schedule, its new ETag and whether it was created.

        Raises:
            HarborValidationError: A field breaks a timetable rule, or the connection
                already has the most schedules allowed.
            UnknownReferenceError: The project or the connection does not exist.
            ConnectionUnavailableError: The connection is being deleted.
            ScheduleIdTakenError: ``If-None-Match: *`` and the schedule exists.
            HarborPreconditionFailedError: ``If-Match`` does not hold.
        """
        scope.require(OPERATIONS["put_schedule"].role)
        view, version, created = await self._app_service.schedule_service.put(
            ScheduleKey(scope.tenant_id, schedule_id),
            _schedule_write(body),
            actor=scope.actor,
            conditions=WriteConditions(
                expected_version=version_from_etag(preconditions.if_match),
                create_only=preconditions.if_none_match == "*",
            ),
        )
        return Saved(ScheduleOut.model_validate(view), etag(version), created)

    async def delete_schedule(self, scope: Scope, schedule_id: str) -> None:
        """Remove a schedule and its timer; a run it started keeps going.

        Raises:
            ScheduleNotFoundError: No such schedule in the tenant.
        """
        scope.require(OPERATIONS["delete_schedule"].role)
        await self._app_service.schedule_service.delete(ScheduleKey(scope.tenant_id, schedule_id))

    async def pause_schedule(
        self, scope: Scope, schedule_id: str, body: SchedulePauseRequest | None
    ) -> Tagged[ScheduleOut]:
        """Pause a schedule; pausing a paused one returns it unchanged.

        Args:
            scope: Caller and tenant; the caller is recorded as who paused it.
            schedule_id: The schedule to pause.
            body: The pause's reason; ``None`` or no reason records a default one.

        Returns:
            The paused schedule and its ETag.

        Raises:
            ScheduleNotFoundError: No such schedule in the tenant.
        """
        scope.require(OPERATIONS["pause_schedule"].role)
        view, version = await self._app_service.schedule_service.pause(
            ScheduleKey(scope.tenant_id, schedule_id),
            reason=body.reason if body is not None else None,
            actor=scope.actor,
        )
        return Tagged(ScheduleOut.model_validate(view), etag(version))

    async def resume_schedule(self, scope: Scope, schedule_id: str) -> Tagged[ScheduleOut]:
        """Resume a schedule and clear its failure count.

        Returns:
            The resumed schedule and its ETag.

        Raises:
            ScheduleNotFoundError: No such schedule in the tenant.
        """
        scope.require(OPERATIONS["resume_schedule"].role)
        view, version = await self._app_service.schedule_service.resume(
            ScheduleKey(scope.tenant_id, schedule_id), actor=scope.actor
        )
        return Tagged(ScheduleOut.model_validate(view), etag(version))

    async def trigger_schedule(self, scope: Scope, schedule_id: str) -> Accepted[IngestionOut]:
        """Start one run now with the schedule's input; the run acts as the schedule.

        Args:
            scope: Caller and tenant.
            schedule_id: The schedule whose input the run takes.

        Returns:
            The queued run and the path that follows it.

        Raises:
            ScheduleNotFoundError: No such schedule in the tenant.
            UnknownReferenceError: The project or the connection no longer exists.
            HarborAlreadyRunningError: The connection already has a run in progress.
            ConnectionUnavailableError: The connection is being deleted.
        """
        scope.require(OPERATIONS["trigger_schedule"].role)
        ingestion = await self._app_service.schedule_service.trigger(
            ScheduleKey(scope.tenant_id, schedule_id)
        )
        return Accepted(
            IngestionOut.model_validate(ingestion),
            f"/v1/admin/ingestions/{ingestion['ingestion_id']}",
        )


def _schedule_write(body: ScheduleWriteRequest) -> ScheduleWrite:
    """Build the service's write from a ``PUT`` body; ``input`` gives the connection and mode."""
    return ScheduleWrite(
        project_id=body.input.project_id,
        connection_id=body.input.connection_id,
        mode=body.input.mode,
        description=body.description,
        interval=body.interval,
        cron=body.cron,
        timezone=body.timezone,
        jitter=body.jitter,
        if_running=body.if_running,
        catch_up=body.catch_up,
        pause_after_failures=body.pause_after_failures,
    )


def _schedule_filter(query: ScheduleListQuery) -> ScheduleFilter:
    """Build the list filters; ``connection_ref`` sets both the project and the connection.

    Raises:
        HarborValidationError: ``connection_ref`` is malformed or names another project
            than ``project_id``.
    """
    project_id, connection_id = query.project_id, None
    if query.connection_ref is not None:
        ref = _connection_ref(query.connection_ref)
        if project_id is not None and project_id != ref.project_id:
            raise HarborValidationError(
                "connection_ref names another project than project_id",
                {"field": "connection_ref"},
            )
        project_id, connection_id = ref.project_id, ref.connection_id
    return ScheduleFilter(
        project_id=project_id,
        connection_id=connection_id,
        operation=query.operation,
        state=query.state,
        due_before=query.due_before,
    )


def _connection_ref(value: str) -> ConnectionRef:
    """Parse ``project_id/connection_id``.

    Raises:
        HarborValidationError: ``value`` is not ``project_id/connection_id``.
    """
    try:
        return ConnectionRef.parse(value)
    except ValueError as error:
        raise HarborValidationError(str(error), {"field": "connection_ref"}) from error
