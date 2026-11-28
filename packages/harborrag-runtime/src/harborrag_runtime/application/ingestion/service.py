"""Postgres-authoritative application service for the public ingestion API."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from harborrag_core.contracts.errors import (
    HarborConflictError,
    HarborConnectionError,
    HarborValidationError,
    IngestionActionStateError,
    IngestionAlreadyCompletedError,
    IngestionAlreadyPausedError,
    IngestionIdempotencyConflictError,
    IngestionNotFoundError,
    IngestionRetryConflictError,
)
from harborrag_core.contracts.events import HarborEvent, event_prefix
from harborrag_core.ingestion import IngestionTask, IngestionTaskState
from harborrag_core.ingestion.run_contracts import (
    IngestionRetryRequest,
    PreparedSourceSubmission,
    RunKind,
    SourceSubmission,
)
from harborrag_core.ingestion.source_query import SourceQuery
from harborrag_core.invariants import require
from harborrag_core.observability.log_events import (
    FailureKind,
    log_event,
    log_failure,
)
from harborrag_engine.ingestion.models import IngestionCreateCommand
from harborrag_engine.ingestion.presenters import (
    STATUS_NAMES,
    TERMINAL_STATES,
    request_hash,
    task_id,
    task_response,
)
from harborrag_engine.ingestion.recovery import retry_from_task, source_from_task
from harborrag_runtime.config.errors import ConnectorConfigurationError
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.errors import (
    WorkflowNotFoundError,
    WorkflowNotRunningError,
    WorkflowOperationError,
    WorkflowSubmissionError,
)

from .ports import (
    ClientProvider,
    PublicTaskStore,
    SourceInputBuilder,
    TaskIdFactory,
    TaskStoreProvider,
)
from .progress_bridge import CONTROL_AUDIT
from .retry_selection import retryable_document_ids

logger = logging.getLogger("harborrag.runtime.application.ingestion.service")

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
    for it. The event sits in the run's event log, where anyone asking "why
    did this stop" already looks; the HTTP event stream does not carry it.
    """
    await store.append_task_event(
        task_id,
        HarborEvent(
            name=f"{event_prefix(task_id)}{CONTROL_AUDIT}",
            trace_id=task_id,
            payload={"action": action, "actor": actor or "unknown"},
        ),
    )


class IngestionApplicationService:
    """Coordinate API persistence and workflow submission, never ingestion work.

    Args:
        settings: Runtime settings; the tenant ID defaults retry requests.
        client_provider: Returns the ingestion gateway that starts and controls workflows.
        task_store_provider: Returns the durable task store.
        source_input_builder: Prepares a submission from the connection it names.
        task_id_factory: Generates task IDs; random UUIDs when omitted.
    """

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
        """Register an ingestion and start its workflow, or return the run its key already made.

        A workflow start that cannot be reached leaves the run pending for recovery.

        Args:
            command: The validated ingestion request.
            idempotency_key: Caller key; a repeat with the same request returns the first run.

        Returns:
            The accepted task's ID, status, message and submission time.

        Raises:
            IngestionIdempotencyConflictError: The key was used for a different request.
            HarborConnectionError: The workflow service could not take the submission.
            HarborValidationError: The connection's settings cannot build its connector.
        """
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
                log_failure(
                    logger,
                    "ingestion.workflow_submission_failed",
                    error,
                    FailureKind.CLASSIFIED,
                    task_id=submitted_source.task_id,
                    tenant_id=submitted_source.tenant_id,
                    connection_id=submitted_source.connection_id,
                )
                raise HarborConnectionError(
                    "Ingestion service is temporarily unavailable."
                ) from error
            await store.update_summary(
                task.task_id,
                {_SUBMISSION_STATE_KEY: _SUBMITTED},
            )
            log_event(
                logger,
                "ingestion.submitted",
                task_id=submitted_source.task_id,
                workflow_id=workflow.workflow_id,
                tenant_id=submitted_source.tenant_id,
                connection_id=submitted_source.connection_id,
                mode="force" if submitted_source.force_reprocess else "incremental",
            )
        else:
            log_event(
                logger,
                "ingestion.existing_run_returned",
                task_id=task.task_id,
                tenant_id=task.request.get("tenant_id"),
                connection_id=task.request.get("connection_id"),
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
        """Return the public document of a run.

        Args:
            task_id: The run to read.

        Returns:
            The run's public document with its progress counts.

        Raises:
            IngestionNotFoundError: No such run.
        """
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
        A task another pass owns, or that started or settled meanwhile, yields no claim
        and is skipped.

        Args:
            limit: Most pending tasks to examine in this pass.

        Returns:
            The number of tasks whose workflow was started.
        """
        store = await self._task_store_provider()
        recovered = 0
        for task in await store.pending_submissions(limit=limit):
            try:
                claim = await store.claim_submission(task.task_id)
                if claim is None:
                    continue
            except Exception as error:  # noqa: BLE001 - one bad task must not starve the outbox
                log_failure(
                    logger,
                    "ingestion.pending_submission_claim_failed",
                    error,
                    FailureKind.UNEXPECTED,
                    task_id=task.task_id,
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
            except Exception as error:  # noqa: BLE001 - one bad task must not starve the outbox
                log_failure(
                    logger,
                    "ingestion.pending_submission_recovery_failed",
                    error,
                    FailureKind.UNEXPECTED,
                    task_id=task.task_id,
                )
                if not start_called:
                    await self._release_claim(store, task.task_id, claim)
                continue
            try:
                await store.update_summary(task.task_id, {_SUBMISSION_STATE_KEY: _SUBMITTED})
            except Exception as error:  # noqa: BLE001 - the run started; a stale claim is re-tried
                log_failure(
                    logger,
                    "ingestion.recovered_submission_unconfirmed",
                    error,
                    FailureKind.UNEXPECTED,
                    task_id=task.task_id,
                )
            recovered += 1
        return recovered

    @staticmethod
    async def _release_claim(store: PublicTaskStore, task_id: str, claim: str) -> None:
        """Give a submission claim back so the next recovery pass can take the task."""
        try:
            await store.release_submission_claim(task_id, claim)
        except Exception as error:  # noqa: BLE001 - the claim goes stale and is retried anyway
            log_failure(
                logger,
                "ingestion.submission_claim_release_failed",
                error,
                FailureKind.UNEXPECTED,
                task_id=task_id,
            )

    async def pause(self, task_id: str, *, actor: str | None = None) -> dict[str, object]:
        """Pause a run's workflow and record who asked.

        Args:
            task_id: The run to pause.
            actor: Principal recorded as asking; ``unknown`` when None.

        Returns:
            The run's ID, its status and a message.

        Raises:
            IngestionNotFoundError: No such run.
            IngestionActionStateError: The run is a retry run, which can only be cancelled.
            IngestionAlreadyCompletedError: The run already ended.
            IngestionAlreadyPausedError: The run is already paused.
            HarborConnectionError: The workflow service could not take the request.
        """
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
        """Resume a paused run's workflow and record who asked.

        A running run is resumed too when its workflow holds a pause it has not
        applied yet, such as one sent during discovery.

        Args:
            task_id: The run to resume.
            actor: Principal recorded as asking; ``unknown`` when None.

        Returns:
            The run's ID, its status and a message.

        Raises:
            IngestionNotFoundError: No such run.
            IngestionActionStateError: The run is a retry run, which can only be cancelled,
                or it is running with no pause pending.
            IngestionAlreadyCompletedError: The run already ended.
            HarborConnectionError: The workflow service could not take the request.
        """
        store = await self._task_store_provider()
        task = await self._required_task(store, task_id)
        _refuse_retry_run(task, "resumed")
        if task.status in TERMINAL_STATES:
            raise IngestionAlreadyCompletedError("The ingestion task is already complete.")
        if task.status is IngestionTaskState.RUNNING and not await self._pause_pending(task_id):
            raise IngestionActionStateError("cannot resume an ingestion that is running")
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

    async def _pause_pending(self, task_id: str) -> bool:
        """Whether the run's workflow reports a pause it has not applied yet.

        The workflow takes a pause at its next batch boundary, so one sent during
        discovery leaves the run RUNNING for hours. An unknown answer counts as none.
        """
        try:
            status = await (await self._client_provider()).get_status(task_id)
        except Exception as error:  # noqa: BLE001 - an unknown answer keeps the already-running refusal
            log_failure(
                logger,
                "ingestion.pause_status_unavailable",
                error,
                FailureKind.EXPECTED,
                task_id=task_id,
            )
            return False
        return status.paused and not status.cancel_requested

    async def cancel(self, task_id: str, *, actor: str | None = None) -> dict[str, object]:
        """Cancel an unfinished run and record who asked.

        A run whose workflow already ended outside HarborRAG is settled instead of cancelled.

        Args:
            task_id: The run to cancel.
            actor: Principal recorded as asking; ``unknown`` when None.

        Returns:
            The run's ID, its status and a message.

        Raises:
            IngestionNotFoundError: No such run.
            IngestionAlreadyCompletedError: The run already ended.
            HarborConnectionError: The workflow service could not take the request.
        """
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

        A run whose workflow is gone or closed, such as one whose history passed the
        namespace retention, is recorded as cancelled here, since no workflow is left
        to record it. So is a queued source run whose workflow start was never
        confirmed; a start that races the cancel is refused by the run's first activity.

        Args:
            store: The run registry.
            task: The run to cancel.

        Returns:
            The run, re-read when it was recorded as cancelled here.

        Raises:
            HarborConnectionError: Temporal could not take the request.
        """
        kind = RunKind.of(task.request)
        try:
            await (await self._client_provider()).cancel(task.task_id, kind)
        except WorkflowOperationError as error:
            reason = _GONE_WORKFLOW_REASONS.get(type(error))
            if reason is None:
                raise HarborConnectionError(
                    "Ingestion cancellation is temporarily unavailable."
                ) from error
            if _may_await_submission(task, kind):
                await self._cancel_unsubmitted(store, task.task_id, reason=reason)
            else:
                await store.close_abandoned(
                    task.task_id, IngestionTaskState.CANCELLED, reason=reason
                )
            return await self._required_task(store, task.task_id)
        return task

    async def _cancel_unsubmitted(
        self, store: PublicTaskStore, task_id: str, *, reason: str
    ) -> None:
        """Record a queued run with no workflow as cancelled, holding its submission claim if free.

        The claim keeps recovery from starting the run meanwhile. A claim another
        pass holds does not stop the cancel: a recovery whose start failed keeps it
        until it goes stale, minutes later, and any start that does go through gets
        no further than the run's first activity, which refuses a cancelled run.
        """
        claim = await store.claim_submission(task_id)
        try:
            await store.close_abandoned(task_id, IngestionTaskState.CANCELLED, reason=reason)
        finally:
            if claim is not None:
                await self._release_claim(store, task_id, claim)

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
        except Exception as error:  # noqa: BLE001 - an unknown answer keeps the run active
            log_event(
                logger,
                "ingestion.workflow_status_unavailable",
                level=logging.DEBUG,
                task_id=task_id,
                error=error,
            )
            return False
        state = _ENDED_WORKFLOW_STATES.get(status)
        if state is None:
            return False
        await store.close_abandoned(task_id, state, reason=f"workflow_{status}")
        log_event(
            logger,
            "ingestion.run_ended_outside_harborrag",
            level=logging.WARNING,
            task_id=task_id,
            workflow_status=status,
            task_status=state.value,
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
        """Start a retry run of a finished run's failed documents.

        Args:
            task_id: The finished run.
            document_ids: Documents to retry; empty retries every retryable failure.
            actor: Principal recorded on the retry run.
            actor_type: ``user`` or ``api_key``.

        Returns:
            The original and retry task IDs and the number of documents accepted.

        Raises:
            IngestionNotFoundError: No such run.
            IngestionRetryConflictError: The run is not finished or has no retryable failures.
            HarborConnectionError: The workflow service could not take the retry.
        """
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
            tenant_id = str(original.request.get("tenant_id") or self._settings.tenant_id)
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

        Args:
            command: The validated ingestion request.

        Returns:
            The provider-neutral input of the new run.

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
        """Return the run, or raise ``IngestionNotFoundError`` when there is none."""
        task = await store.get(task_id)
        if task is None:
            raise IngestionNotFoundError("Ingestion task was not found.")
        return task


def _refuse_retry_run(task: IngestionTask, past: str) -> None:
    """Raise for a retry run: its workflow can be cancelled but not paused or resumed.

    Args:
        task: The run an action was asked for.
        past: The refused action in the past tense, such as ``paused``.

    Raises:
        IngestionActionStateError: ``task`` is a retry run.
    """
    if RunKind.of(task.request) is RunKind.RETRY:
        raise IngestionActionStateError(f"retry runs cannot be {past}; cancel the run instead")


def _may_await_submission(task: IngestionTask, kind: RunKind) -> bool:
    """Whether ``task`` is a queued source run whose workflow start was never confirmed."""
    return (
        kind is RunKind.SOURCE
        and task.status is IngestionTaskState.PENDING
        and task.summary.get(_SUBMISSION_STATE_KEY) != _SUBMITTED
    )
