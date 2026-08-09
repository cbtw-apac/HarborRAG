from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic_core import to_jsonable_python

from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.temporal.client import IngestionTemporalClient
from harborrag_runtime.temporal.schemas import SourceIngestionInput, SourceQuery
from harborrag_runtime.temporal.submission import SourceSubmission

from ..errors import failure_response
from ..schemas import AppResponse

if TYPE_CHECKING:
    from ..composition.factories import TaskRegistry

type RuntimeClientProvider = Callable[[], Awaitable[IngestionTemporalClient]]
type TaskRegistryProvider = Callable[[], Awaitable["TaskRegistry"]]
type SourceInputBuilder = Callable[
    [RuntimeSettings, SourceSubmission],
    SourceIngestionInput,
]

logger = logging.getLogger("harborrag.app.workflow_control.ingestion.temporal")


class TemporalIngestionOperations:
    """Transport-neutral Temporal ingestion operations.

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
                ),
            )
            await (await self._source_task_registry()).register(request)
            client = await self._runtime_client()
            reference = await client.start_ingestion(request)
        except Exception as exc:  # noqa: BLE001 - stable error envelope
            return failure_response(logger, exc, "start ingestion run %r", run_id)
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
        except Exception as exc:  # noqa: BLE001 - stable error envelope
            return failure_response(logger, exc, "read status for ingestion run %r", run_id)

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
