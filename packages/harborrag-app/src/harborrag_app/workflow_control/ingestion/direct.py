"""Direct (in-process) ingestion behind ``harborrag ingest run``.

Mirrors TemporalIngestionOperations for the SDK's ExecutionMode.DIRECT: the run happens
inside this process through the same use-case graph the worker uses, so it needs Qdrant,
FalkorDB and the object store but neither Temporal nor a worker.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import asdict
from uuid import uuid4

from harborrag_core.schemas.ids import TenantId
from harborrag_core.security import AccessContext
from harborrag_runtime.sdk import HarborRAG, IngestionRequest

from ..errors import failure_response
from ..schemas import AppResponse

logger = logging.getLogger("harborrag.app.workflow_control.ingestion.direct")

CLI_PRINCIPAL = "harborrag-cli"


def new_run_id() -> str:
    return f"ingest-{uuid4().hex}"


class DirectIngestionOperations:
    def __init__(self, runtime_sdk: Callable[[], HarborRAG]) -> None:
        self._runtime_sdk = runtime_sdk

    async def run_ingestion(  # noqa: PLR0913 - stable service port
        self,
        *,
        tenant_id: str,
        connector_name: str,
        run_id: str,
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
    ) -> AppResponse:
        try:
            request = IngestionRequest(
                access=AccessContext(principal_id=CLI_PRINCIPAL, tenant_id=TenantId(tenant_id)),
                connector_name=connector_name,
                task_id=run_id,
                connection_id=connection_id,
                source_scope_id=source_scope_id,
                path=path,
                pattern=pattern,
                recursive=recursive,
                updated_after=updated_after,
                limit=max_artifacts,
                include_attachments=include_attachments,
                filters=dict(filters or {}),
                force_reprocess=force_reprocess,
            )
            result = await self._runtime_sdk().ingestion.run(request)
        except Exception as exc:  # noqa: BLE001 - stable error envelope
            return failure_response(logger, exc, "run ingestion %r", run_id)
        logger.info("Direct ingestion run %r finished with status %r", run_id, result.status)
        return AppResponse(True, {"result": asdict(result)})


class DirectIngestionClientMixin:
    _direct: DirectIngestionOperations

    async def run_ingestion(  # noqa: PLR0913 - stable service port
        self,
        *,
        tenant_id: str,
        connector_name: str,
        run_id: str,
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
    ) -> AppResponse:
        return await self._direct.run_ingestion(
            tenant_id=tenant_id,
            connector_name=connector_name,
            run_id=run_id,
            connection_id=connection_id,
            source_scope_id=source_scope_id,
            path=path,
            pattern=pattern,
            recursive=recursive,
            updated_after=updated_after,
            max_artifacts=max_artifacts,
            include_attachments=include_attachments,
            filters=filters,
            force_reprocess=force_reprocess,
        )


__all__ = [
    "CLI_PRINCIPAL",
    "DirectIngestionClientMixin",
    "DirectIngestionOperations",
    "new_run_id",
]
