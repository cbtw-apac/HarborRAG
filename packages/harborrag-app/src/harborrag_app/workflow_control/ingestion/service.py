"""Postgres-authoritative application service for the public ingestion API."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from harborrag_core.contracts.errors import (
    HarborConflictError,
    HarborConnectionError,
    HarborValidationError,
)
from harborrag_core.contracts.events import HarborEvent
from harborrag_core.ingestion import IngestionTask, IngestionTaskState
from harborrag_core.invariants import require
from harborrag_runtime.config.errors import ConnectorConfigurationError
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.errors import (
    WorkflowNotFoundError,
    WorkflowNotRunningError,
    WorkflowOperationError,
    WorkflowSubmissionError,
)
from harborrag_runtime.ingestion_contracts import (
    IngestionRetryRequest,
    PreparedSourceSubmission,
    RunKind,
    SourceSubmission,
)
from harborrag_runtime.source_query import SourceQuery

from ..errors import (
    IngestionActionStateError,
    IngestionAlreadyCompletedError,
    IngestionAlreadyPausedError,
    IngestionAlreadyRunningError,
    IngestionIdempotencyConflictError,
    IngestionNotFoundError,
    IngestionRetryConflictError,
)
from .models import IngestionCreateCommand
from .ports import (
    ClientProvider,
    PublicTaskStore,
    SourceInputBuilder,
    TaskIdFactory,
    TaskStoreProvider,
)
from .presenters import (
    STATUS_NAMES,
    TERMINAL_STATES,
    request_hash,
    task_id,
    task_response,
)
from .recovery import retry_from_task, source_from_task
from .retry_selection import retryable_document_ids

logger = logging.getLogger("harborrag.app.workflow_control.ingestion")

_SUBMISSION_STATE_KEY = "submission_state"
_SUBMITTED = "submitted"
_WORKFLOW_LOOKUP_SECONDS = 5.0
_ENDED_WORKFLOW_STATES = {
    "terminated": IngestionTaskState.CANCELLED,
    "canceled": IngestionTaskState.CANCELLED,
    "failed": IngestionTaskState.FAILED,
    "timed_out": IngestionTaskState.FAILED,
    "completed": IngestionTaskState.FAILED,
}
_GONE_WORKFLOW_REASONS: dict[type[WorkflowOperationError], str] = {
    WorkflowNotFoundError: "workflow_not_found",
    WorkflowNotRunningError: "workflow_closed",
}


async def record_control_action(
    store: PublicTaskStore, task_id: str, *, action: str, actor: str | None
) -> None:
    """Append who asked for pause/resume/cancel to the run's own event trail.

    ``cancelled_at_safe_boundary`` used to be the only trace a cancellation
    left, with nothing saying whether an operator, the API or a script asked
    for it. The event sits next to the run's progress events, where anyone
    asking "why did this stop" already looks.
    """

    await store.append_task_event(
        task_id,
        HarborEvent(
            name=f"task.{task_id}.control",
            trace_id=task_id,
            payload={"action": action, "actor": actor or "unknown"},
        ),
    )


class IngestionApplicationService:
    """Coordinate API persistence and workflow submission, never ingestion work."""

    def __init__(
        self,
        settings: RuntimeSettings,
        *,
        client_provider: ClientProvider,
        task_store_provider: TaskStoreProvider,
        source_input_builder: SourceInputBuilder,
        task_id_factory: TaskIdFactory | None = None,
    ) -> None:
        self._settings = settings
        self._client_provider = client_provider
        self._task_store_provider = task_store_provider
        self._source_input_builder = source_input_builder
        self._task_id_factory = task_id_factory or task_id

    async def submit(
        self,
        command: IngestionCreateCommand,
        *,
        idempotency_key: str | None,
    ) -> dict[str, object]:
        source = await self._build_source(command)
        hashed_request = request_hash(command.public_request)
        store = await self._task_store_provider()
        try:
            registration = await store.register(
                source,
                idempotency_key=idempotency_key,
                request_hash=hashed_request if idempotency_key is not None else None,
                actor=command.actor,
                actor_type=command.actor_type,
            )
        except HarborConflictError as error:
            raise IngestionIdempotencyConflictError(str(error)) from error
        task = registration.task
        if (
            task.status is IngestionTaskState.PENDING
            and task.summary.get(_SUBMISSION_STATE_KEY) != _SUBMITTED
        ):
            submitted_source = source if registration.created else source_from_task(task)
            try:
                workflow = await (await self._client_provider()).start_ingestion(submitted_source)
            except WorkflowSubmissionError as error:
                logger.error(
                    "Ingestion workflow submission failed task_id=%s tenant=%s connection_id=%s",
                    submitted_source.task_id,
                    submitted_source.tenant_id,
                    submitted_source.connection_id,
                )
                raise HarborConnectionError(
                    "Ingestion service is temporarily unavailable."
                ) from error
            await store.update_summary(
                task.task_id,
                {_SUBMISSION_STATE_KEY: _SUBMITTED},
            )
            logger.info(
                "Ingestion submitted task_id=%s workflow_id=%s tenant=%s "
                "connection_id=%s force_reprocess=%s",
                submitted_source.task_id,
                workflow.workflow_id,
                submitted_source.tenant_id,
                submitted_source.connection_id,
                submitted_source.force_reprocess,
            )
        else:
            logger.info(
                "Existing ingestion returned task_id=%s tenant=%s connection_id=%s",
                task.task_id,
                task.request.get("tenant_id"),
                task.request.get("connection_id"),
            )
        submitted_at = require(
            task.submitted_at,
            "registered ingestion task is missing submitted_at",
        )
        return {
            "task_id": task.task_id,
            "status": "PENDING",
            "message": (
                "Ingestion task accepted"
                if registration.created
                else "Existing ingestion task returned"
            ),
            "submitted_at": submitted_at,
        }

    async def get_task(self, task_id: str) -> dict[str, object]:
        store = await self._task_store_provider()
        task = await self._required_task(store, task_id)
        counts = await store.progress(task_id)
        return task_response(task, counts)

    async def recover_pending_submissions(self, *, limit: int = 100) -> int:
        """Restart durable tasks left between database commit and execution start.

        Each task is claimed under its row lock before the start, so the submit path
        cannot fail it in between (a started run would then hit FAILED -> RUNNING).
        The claim is released only when nothing can have started (no client, or a
        failure before the start call). A start call that fails is ambiguous: the
        workflow may exist. Its claim, like that of a pass that dies mid-start,
        stays until it is stale; the store then hands the task out again, and the
        retry is idempotent because a start treats an existing workflow id as success.
        """

        store = await self._task_store_provider()
        recovered = 0
        for task in await store.pending_submissions(limit=limit):
            try:
                claim = await store.claim_submission(task.task_id)
                if claim is None:
                    continue  # another pass owns it, or it started or settled meanwhile
            except Exception:  # noqa: BLE001 - one bad task must not starve the outbox
                logger.exception(
                    "Pending ingestion submission claim failed task_id=%s", task.task_id
                )
                continue
            start_called = False
            try:
                client = await self._client_provider()
                if RunKind.of(task.request) is RunKind.RETRY:
                    retry = retry_from_task(task)
                    start_called = True
                    await client.start_retry_failures(retry)
                else:
                    source = source_from_task(task)
                    start_called = True
                    await client.start_ingestion(source)
            except Exception:  # noqa: BLE001 - one bad task must not starve the outbox
                logger.exception(
                    "Pending ingestion submission recovery failed task_id=%s",
                    task.task_id,
                )
                if not start_called:
                    await self._release_claim(store, task.task_id, claim)
                continue
            try:
                await store.update_summary(task.task_id, {_SUBMISSION_STATE_KEY: _SUBMITTED})
            except Exception:  # noqa: BLE001 - the run started; a stale claim is re-tried
                logger.exception(
                    "Could not confirm the recovered submission task_id=%s", task.task_id
                )
            recovered += 1
        return recovered

    @staticmethod
    async def _release_claim(store: PublicTaskStore, task_id: str, claim: str) -> None:
        try:
            await store.release_submission_claim(task_id, claim)
        except Exception:  # noqa: BLE001 - the claim goes stale and is retried anyway
            logger.exception("Could not release the submission claim task_id=%s", task_id)

    async def pause(self, task_id: str, *, actor: str | None = None) -> dict[str, object]:
        store = await self._task_store_provider()
        task = await self._required_task(store, task_id)
        _refuse_retry_run(task, "paused")
        if task.status in TERMINAL_STATES:
            raise IngestionAlreadyCompletedError("The ingestion task is already complete.")
        if task.status is IngestionTaskState.PAUSED:
            raise IngestionAlreadyPausedError("The ingestion task is already paused.")
        try:
            await (await self._client_provider()).pause(task_id)
        except WorkflowOperationError as error:
            raise HarborConnectionError("Ingestion pause is temporarily unavailable.") from error
        await record_control_action(store, task_id, action="pause", actor=actor)
        return {
            "task_id": task.task_id,
            "status": STATUS_NAMES[task.status],
            "message": "Pause requested",
        }

    async def resume(self, task_id: str, *, actor: str | None = None) -> dict[str, object]:
        store = await self._task_store_provider()
        task = await self._required_task(store, task_id)
        _refuse_retry_run(task, "resumed")
        if task.status in TERMINAL_STATES:
            raise IngestionAlreadyCompletedError("The ingestion task is already complete.")
        if task.status is IngestionTaskState.RUNNING:
            raise IngestionAlreadyRunningError("The ingestion task is already running.")
        try:
            await (await self._client_provider()).resume(task_id)
        except WorkflowOperationError as error:
            raise HarborConnectionError("Ingestion resume is temporarily unavailable.") from error
        await record_control_action(store, task_id, action="resume", actor=actor)
        return {
            "task_id": task.task_id,
            "status": STATUS_NAMES[IngestionTaskState.RUNNING],
            "message": "Resume requested",
        }

    async def cancel(self, task_id: str, *, actor: str | None = None) -> dict[str, object]:
        store = await self._task_store_provider()
        task = await self._required_task(store, task_id)
        if task.status in TERMINAL_STATES:
            raise IngestionAlreadyCompletedError("The ingestion task is already complete.")
        if await self.settle_if_ended(task_id):
            task = await self._required_task(store, task_id)
        else:
            task = await self._cancel_workflow(store, task)
        await record_control_action(store, task_id, action="cancel", actor=actor)
        return {
            "task_id": task.task_id,
            "status": STATUS_NAMES[task.status],
            "message": "Cancellation requested",
        }

    async def _cancel_workflow(self, store: PublicTaskStore, task: IngestionTask) -> IngestionTask:
        """Ask the run's workflow to cancel and return the run as it now stands.

        A retry run whose workflow is gone or closed is recorded as cancelled here,
        since no workflow is left to record it.

        Raises:
            HarborConnectionError: Temporal could not take the request.
        """
        kind = RunKind.of(task.request)
        try:
            await (await self._client_provider()).cancel(task.task_id, kind)
        except WorkflowOperationError as error:
            reason = _GONE_WORKFLOW_REASONS.get(type(error))
            if kind is not RunKind.RETRY or reason is None:
                raise HarborConnectionError(
                    "Ingestion cancellation is temporarily unavailable."
                ) from error
            await store.close_abandoned(task.task_id, IngestionTaskState.CANCELLED, reason=reason)
            return await self._required_task(store, task.task_id)
        return task

    async def settle_if_ended(self, task_id: str) -> bool:
        """Record the end of a run whose workflow ended without recording it.

        A workflow terminated in Temporal never runs the step that records its end,
        so its run would stay active and block its connection. The run's own workflow
        is described: ``harborrag-retry:<id>`` for a retry run, ``harborrag-source:<id>``
        otherwise. Only a workflow Temporal reports as ended settles a run; any other
        answer, paused or not found included, leaves it active, since a queued run may
        not be submitted yet.

        Args:
            task_id: The run to check.

        Returns:
            True when the run is over and its record says so; False while it may run.
        """
        store = await self._task_store_provider()
        task = await store.get(task_id)
        if task is None or task.status in TERMINAL_STATES:
            return True
        try:
            async with asyncio.timeout(_WORKFLOW_LOOKUP_SECONDS):
                status = await (await self._client_provider()).execution_status(
                    task_id, RunKind.of(task.request)
                )
        except Exception:  # noqa: BLE001 - an unknown answer keeps the run active
            logger.debug("Could not read the workflow of ingestion %s", task_id, exc_info=True)
            return False
        state = _ENDED_WORKFLOW_STATES.get(status)
        if state is None:
            return False
        await store.close_abandoned(task_id, state, reason=f"workflow_{status}")
        logger.warning(
            "Ingestion %s ended outside HarborRAG (workflow %s); recorded as %s",
            task_id,
            status,
            state.value,
        )
        return True

    async def retry_failures(
        self,
        *,
        task_id: str,
        document_ids: Sequence[str],
        actor: str | None = None,
        actor_type: str | None = None,
    ) -> dict[str, object]:
        store = await self._task_store_provider()
        original = await self._required_task(store, task_id)
        if original.status not in TERMINAL_STATES:
            raise IngestionRetryConflictError(
                "Failed documents can be retried after the ingestion task is complete."
            )
        selected = await retryable_document_ids(
            store,
            task_id=task_id,
            requested=document_ids,
        )
        if not selected:
            raise IngestionRetryConflictError(
                "The ingestion task has no matching retryable document failures."
            )
        retry_task_id = self._task_id_factory()
        registration = await store.register_retry(
            retry_task_id=retry_task_id,
            original=original,
            document_ids=selected,
            actor=actor,
            actor_type=actor_type,
        )
        try:
            tenant_id = str(original.request.get("tenant_id") or self._settings.ingestion_tenant_id)
            retry = IngestionRetryRequest(
                retry_task_id=retry_task_id,
                original_task_id=task_id,
                tenant_id=tenant_id,
                document_ids=tuple(selected),
            )
            await (await self._client_provider()).start_retry_failures(retry)
        except WorkflowSubmissionError as error:
            raise HarborConnectionError(
                "Ingestion retry service is temporarily unavailable."
            ) from error
        await store.update_summary(
            registration.task.task_id,
            {_SUBMISSION_STATE_KEY: _SUBMITTED},
        )
        return {
            "task_id": task_id,
            "retry_task_id": registration.task.task_id,
            "accepted_document_count": len(selected),
            "message": "Failed documents accepted for retry",
        }

    async def _build_source(self, command: IngestionCreateCommand) -> PreparedSourceSubmission:
        """Prepare the submission of the connection ``command`` names.

        Raises:
            HarborValidationError: The connection's settings cannot build its connector.
        """
        try:
            source = await self._source_input_builder(
                self._settings,
                SourceSubmission(
                    task_id=self._task_id_factory(),
                    tenant_id=command.tenant_id,
                    connector_name=command.connection_id,
                    connection_id=command.connection_id,
                    source_scope_id=command.source_scope_id,
                    force_reprocess=command.force_reprocess,
                    query=SourceQuery(updated_after=command.updated_after),
                ),
            )
        except ConnectorConfigurationError as error:
            raise HarborValidationError(str(error)) from error
        return source

    @staticmethod
    async def _required_task(
        store: PublicTaskStore,
        task_id: str,
    ) -> IngestionTask:
        task = await store.get(task_id)
        if task is None:
            raise IngestionNotFoundError("Ingestion task was not found.")
        return task


def _refuse_retry_run(task: IngestionTask, past: str) -> None:
    """Raise for a retry run: its workflow can be cancelled but not paused or resumed.

    Raises:
        IngestionActionStateError: ``task`` is a retry run.
    """
    if RunKind.of(task.request) is RunKind.RETRY:
        raise IngestionActionStateError(f"retry runs cannot be {past}; cancel the run instead")
