from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic_core import to_jsonable_python

from harborrag_core.contracts.errors import HarborNotFoundError
from harborrag_core.ingestion import IngestionTask, IngestionTaskState
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.errors import (
    RuntimeConnectionError,
    WorkflowNotFoundError,
    WorkflowRunAlreadyStartedError,
    WorkflowSubmissionError,
)
from harborrag_runtime.ingestion_contracts import (
    IngestionExecutionReference,
    IngestionGateway,
    PreparedSourceSubmission,
    SourceSubmission,
)
from harborrag_runtime.source_query import SourceQuery

from ..errors import failure_response
from ..schemas import AppResponse
from .presenters import TERMINAL_STATES, task_response
from .service import record_control_action

if TYPE_CHECKING:
    from ..composition.factories import TaskRegistry

type RuntimeClientProvider = Callable[[], Awaitable[IngestionGateway]]
type TaskRegistryProvider = Callable[[], Awaitable["TaskRegistry"]]
type SourceInputBuilder = Callable[
    [RuntimeSettings, SourceSubmission],
    PreparedSourceSubmission,
]

logger = logging.getLogger("harborrag.app.workflow_control.ingestion.durable")


def _operator() -> str:
    """The local operator, for the run's event trail; access control is the DB credential."""

    import getpass
    import socket

    return f"{getpass.getuser()}@{socket.gethostname()}"


class DurableIngestionOperations:
    """Transport- and execution-provider-neutral ingestion operations.

    A collaborator rather than a mixin: it declares the settings, client, registry, and
    builder it needs, so mypy checks those uses instead of them being attributes it hopes
    the host class happens to define.
    """

    def __init__(
        self,
        settings: RuntimeSettings,
        *,
        runtime_client: RuntimeClientProvider,
        task_registry: TaskRegistryProvider,
        source_input_builder: SourceInputBuilder,
    ) -> None:
        self._settings = settings
        self._runtime_client = runtime_client
        self._source_task_registry = task_registry
        self._source_input_builder = source_input_builder

    async def start_ingestion(  # noqa: PLR0913 - stable service port
        self,
        *,
        tenant_id: str,
        connector_name: str,
        run_id: str | None = None,
        connection_id: str | None = None,
        source_scope_id: str | None = None,
        path: str | None = None,
        pattern: str | None = None,
        recursive: bool = True,
        updated_after: str | None = None,
        max_artifacts: int | None = None,
        include_attachments: bool = True,
        filters: Mapping[str, object] | None = None,
        force_reprocess: bool = False,
        batch_size: int | None = None,
        document_concurrency: int | None = None,
        wait: bool = False,
    ) -> AppResponse:
        suffix = uuid4().hex
        run_id = run_id or f"ingestion-{suffix}"
        try:
            request = self._source_input_builder(
                self._settings,
                SourceSubmission(
                    task_id=run_id,
                    tenant_id=tenant_id,
                    connector_name=connector_name,
                    connection_id=connection_id,
                    source_scope_id=source_scope_id,
                    query=SourceQuery(
                        path=path,
                        pattern=pattern,
                        recursive=recursive,
                        updated_after=updated_after,
                        limit=max_artifacts,
                        include_attachments=include_attachments,
                        filters_json=json.dumps(
                            dict(filters or {}),
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                    ),
                    force_reprocess=force_reprocess,
                    batch_size=batch_size,
                    document_concurrency=document_concurrency,
                ),
            )
            registry = await self._source_task_registry()
            registration = await registry.register(request)
        except Exception as exc:  # noqa: BLE001 - stable error envelope
            return failure_response(logger, exc, "start ingestion run %r", run_id)
        submitted = await self._submit(registry, registration.task, request)
        if isinstance(submitted, AppResponse):
            return submitted
        client, reference = submitted
        logger.info(
            "Started ingestion run %r for tenant %r using connector %r as workflow %r",
            run_id,
            tenant_id,
            connector_name,
            reference.workflow_id,
        )
        data: dict[str, object] = {
            "run": {
                "run_id": request.task_id,
                "tenant_id": request.tenant_id,
                "connector_name": request.connector_name,
                "connector_type": request.connector_type,
                "connection_id": request.connection_id,
                "source_scope_id": request.source_scope_id,
                "status": "PENDING",
            },
            "workflow": to_jsonable_python(reference),
        }
        if not wait:
            return AppResponse(True, data)
        try:
            data["result"] = to_jsonable_python(await client.result(run_id))
        except Exception as exc:  # noqa: BLE001 - stable error envelope
            failure = failure_response(logger, exc, "await result for ingestion run %r", run_id)
            return AppResponse(False, {**data, **failure.data}, failure.error)
        return AppResponse(True, data)

    async def _submit(
        self,
        registry: TaskRegistry,
        task: IngestionTask,
        request: PreparedSourceSubmission,
    ) -> tuple[IngestionGateway, IngestionExecutionReference] | AppResponse:
        """Start a registered run, or answer why it did not start and where it stands."""

        run_id = task.task_id
        if task.status in TERMINAL_STATES:
            # A finished run cannot start again; Temporal would accept the ID and run
            # a workflow whose first task transition the control plane then refuses.
            return failure_response(
                logger,
                WorkflowRunAlreadyStartedError(
                    f"run id {run_id!r} already used ({task.status.value})"
                ),
                "start ingestion run %r",
                run_id,
            )
        status = task.status.value
        try:
            client = await self._runtime_client()
        except Exception as exc:  # noqa: BLE001 - stable error envelope
            failure = failure_response(logger, exc, "connect to start ingestion run %r", run_id)
            if (
                isinstance(exc, RuntimeConnectionError)
                and task.status is IngestionTaskState.PENDING
                and await self._fail_unsubmitted(registry, run_id)
            ):
                # Nothing was submitted, so the run can never start on its own; left
                # PENDING it would look queued forever and later start unasked.
                status = IngestionTaskState.FAILED.value
            return _run_failure(run_id, status, failure)
        try:
            reference = await client.start_ingestion(request)
        except WorkflowRunAlreadyStartedError as exc:
            return failure_response(logger, exc, "start ingestion run %r", run_id)
        except Exception as exc:  # noqa: BLE001 - stable error envelope
            failure = failure_response(logger, exc, "start ingestion run %r", run_id)
            if isinstance(exc, WorkflowSubmissionError):
                # The request may have reached Temporal before the call failed, so the
                # run stays PENDING for the outbox and the operator learns how to check.
                reason = str(failure.error or type(exc).__name__).rstrip(".")
                failure = AppResponse(
                    False,
                    failure.data,
                    f"{reason}. The run may still start; check with: "
                    f"harborrag ingest status {run_id}",
                )
            return _run_failure(run_id, status, failure)
        try:
            await registry.update_summary(run_id, {"submission_state": "submitted"})
        except Exception:  # noqa: BLE001 - the run started; the outbox re-submit is harmless
            logger.warning(
                "Could not record the submission of ingestion run %r", run_id, exc_info=True
            )
        return client, reference

    @staticmethod
    async def _fail_unsubmitted(registry: TaskRegistry, run_id: str) -> bool:
        """Mark a never-submitted run FAILED; the connect error stays the one reported."""

        try:
            await registry.transition(
                run_id,
                IngestionTaskState.FAILED,
                summary={"failed_stage": "submission", "error_code": "submission_failed"},
            )
        except Exception:  # noqa: BLE001 - best effort; the outbox can still submit it
            logger.warning(
                "Could not mark unsubmitted ingestion run %r failed", run_id, exc_info=True
            )
            return False
        return True

    async def ingestion_status(self, run_id: str) -> AppResponse:
        try:
            client = await self._runtime_client()
            status, progress, execution_status = await asyncio.gather(
                client.get_status(run_id),
                client.get_progress(run_id),
                client.execution_status(run_id),
            )
            status_payload = to_jsonable_python(status)
            status_payload["run_id"] = status_payload.pop("task_id")
            completed = (
                progress.get("published", 0)
                + progress.get("unchanged", 0)
                + progress.get("failed", 0)
            )
            return AppResponse(
                True,
                {
                    "status": status_payload,
                    "execution_status": execution_status,
                    "progress": {
                        **progress,
                        "processed": completed,
                        "succeeded": progress.get("published", 0),
                    },
                },
            )
        except WorkflowNotFoundError as exc:
            fallback = await self._control_plane_status(run_id)
            if fallback is not None:
                return AppResponse(True, fallback)
            return failure_response(logger, exc, "read status for ingestion run %r", run_id)
        except Exception as exc:  # noqa: BLE001 - stable error envelope
            return failure_response(logger, exc, "read status for ingestion run %r", run_id)

    async def _control_plane_status(self, run_id: str) -> dict[str, object] | None:
        """The recorded state of a run Temporal does not know, e.g. one never submitted.

        Shaped like the Temporal status envelope so status renderers keep working;
        ``execution_status`` says there is no execution behind it.
        """

        try:
            registry = await self._source_task_registry()
            task = await registry.get(run_id)
            if task is None:
                return None
            payload = task_response(task, await registry.progress(run_id))
        except Exception:  # noqa: BLE001 - fall back to the original not-found answer
            logger.warning(
                "Could not read the control-plane record of ingestion run %r",
                run_id,
                exc_info=True,
            )
            return None
        return {
            "status": {
                "run_id": task.task_id,
                "status": task.status.value,
                "paused": task.status is IngestionTaskState.PAUSED,
                "cancel_requested": False,
            },
            "execution_status": "not_found",
            "source": "control_plane",
            "progress": payload["progress"],
            "task": payload,
        }

    async def ingestion_result(self, run_id: str) -> AppResponse:
        try:
            result = await (await self._runtime_client()).result(run_id)
            return AppResponse(True, {"result": to_jsonable_python(result)})
        except Exception as exc:  # noqa: BLE001 - stable error envelope
            return failure_response(logger, exc, "await result for ingestion run %r", run_id)

    async def control_ingestion(self, run_id: str, action: str) -> AppResponse:
        try:
            client = await self._runtime_client()
            if action == "pause":
                await client.pause(run_id)
            elif action == "resume":
                await client.resume(run_id)
            elif action == "cancel":
                await client.cancel(run_id)
            else:
                raise ValueError(f"unsupported ingestion action: {action!r}")
            try:
                await record_control_action(
                    await self._source_task_registry(), run_id, action=action, actor=_operator()
                )
            except HarborNotFoundError:
                # The event trail is audit only; a run with no local control-plane
                # row (another database, or a purged task) still took the action.
                logger.warning(
                    "Recorded no %r event for ingestion run %r: no local task row",
                    action,
                    run_id,
                )
            logger.info("Applied %r to ingestion run %r", action, run_id)
            return AppResponse(True, {"run_id": run_id, "action": action})
        except Exception as exc:  # noqa: BLE001 - stable error envelope
            return failure_response(
                logger,
                exc,
                "apply %r to ingestion run %r",
                action,
                run_id,
            )


def _run_failure(run_id: str, status: str, failure: AppResponse) -> AppResponse:
    """A start failure that still names the run and the state it was left in."""

    return AppResponse(
        False,
        {"run": {"run_id": run_id, "status": status}, **failure.data},
        failure.error,
    )
