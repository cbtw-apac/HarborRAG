"""Application use cases backed by the HarborRAG Temporal runtime client."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from uuid import uuid4

from pydantic_core import to_jsonable_python

from harborrag_core.contracts.errors import HarborUnavailableError
from harborrag_runtime.composition import CompositionRoot, ControlPlaneRepositories
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.retrieval import RuntimeRetrievalService
from harborrag_runtime.temporal.client import TemporalRuntimeClient
from harborrag_runtime.temporal.schemas import IngestionRunInput

from .errors import public_error_message
from .ports import BaseAppService
from .reads import ControlPlaneReadsMixin
from .schemas import AppResponse

type ClientFactory = Callable[
    [TemporalRuntimeConfig],
    Awaitable[TemporalRuntimeClient],
]
type RetrievalFactory = Callable[
    [RuntimeSettings],
    Awaitable[RuntimeRetrievalService],
]

logger = logging.getLogger("harborrag.app.workflow_control.client")


class AppService(ControlPlaneReadsMixin, BaseAppService):
    """Keep transport concerns outside the canonical Temporal ingestion path."""

    def __init__(
        self,
        composition: CompositionRoot,
        settings: RuntimeSettings | None = None,
        *,
        client_factory: ClientFactory = TemporalRuntimeClient.connect,
        retrieval_factory: RetrievalFactory = RuntimeRetrievalService.connect,
    ) -> None:
        self._composition = composition
        self._settings = settings or RuntimeSettings()
        self._runtime_config = TemporalRuntimeConfig.from_settings(self._settings)
        self._client_factory = client_factory
        self._retrieval_factory = retrieval_factory
        self._client: TemporalRuntimeClient | None = None
        self._retrieval: RuntimeRetrievalService | None = None
        self._client_lock = asyncio.Lock()
        self._retrieval_lock = asyncio.Lock()

    def _control_plane(self) -> ControlPlaneRepositories:
        control_plane = self._composition.control_plane
        if control_plane is None:
            raise HarborUnavailableError("control-plane database is not configured")
        return control_plane

    def health(self) -> AppResponse:
        diagnostics = self._composition.diagnostics()
        runtime = diagnostics.get("runtime")
        ready = bool(runtime.get("ready")) if isinstance(runtime, dict) else False
        return AppResponse(
            ok=ready,
            data={"diagnostics": diagnostics},
            error=None if ready else "runtime not ready",
        )

    def ingest_once(self) -> AppResponse:
        return AppResponse(
            False,
            error="use 'harborrag ingest start' to submit the Temporal ingestion workflow",
        )

    async def runtime_health(self) -> AppResponse:
        try:
            async with asyncio.timeout(self._settings.temporal_health_timeout_seconds):
                client = await self._runtime_client()
                ready = await client.health()
            return AppResponse(
                ready,
                {
                    "runtime": {
                        "provider": "temporal",
                        "ready": ready,
                        "target": self._runtime_config.connection.target,
                        "namespace": self._runtime_config.connection.namespace,
                    }
                },
                None if ready else "Temporal workflow service is not ready",
            )
        except Exception as exc:  # noqa: BLE001 - service returns a stable error envelope
            return self._failure(exc, "check Temporal runtime health")

    async def start_ingestion(
        self,
        *,
        tenant_id: str,
        connector_name: str,
        run_id: str | None = None,
        manifest_id: str | None = None,
        generation_id: str | None = None,
        max_artifacts: int | None = None,
        wait: bool = False,
    ) -> AppResponse:
        suffix = uuid4().hex
        run_id = run_id or f"ingestion-{suffix}"
        manifest_id = manifest_id or f"manifest-{suffix}"
        generation_id = generation_id or f"generation-{suffix}"
        request = IngestionRunInput(
            run_id=run_id,
            tenant_id=tenant_id,
            connector_name=connector_name,
            manifest_id=manifest_id,
            generation_id=generation_id,
            options=replace(
                self._runtime_config.workflow_options(),
                max_artifacts=max_artifacts,
            ),
        )
        try:
            client = await self._runtime_client()
            reference = await client.start_ingestion(request)
        except Exception as exc:  # noqa: BLE001 - service returns a stable error envelope
            return self._failure(exc, "start ingestion run %r", run_id)
        logger.info(
            "Started ingestion run %r for tenant %r using connector %r as workflow %r",
            run_id,
            tenant_id,
            connector_name,
            reference.workflow_id,
        )
        data = {
            "run": to_jsonable_python(request),
            "workflow": to_jsonable_python(reference),
        }
        if not wait:
            return AppResponse(True, data)
        # The run is already submitted, so a failed or cancelled outcome must
        # still return the workflow reference: without it the caller has no ID
        # to inspect or retry the run they just started.
        try:
            data["result"] = to_jsonable_python(await client.result(run_id))
        except Exception as exc:  # noqa: BLE001 - service returns a stable error envelope
            failure = self._failure(exc, "await result for ingestion run %r", run_id)
            return AppResponse(False, {**data, **failure.data}, failure.error)
        return AppResponse(True, data)

    async def ingestion_status(self, run_id: str) -> AppResponse:
        """Combine the workflow's self-reported state with Temporal's own view.

        ``status`` is what the workflow tracks about itself and stays "running"
        when the workflow crashes, because a crashed workflow never records its
        own failure. ``execution_status`` is the server-side truth, so callers
        polling for completion have something that actually reaches a terminal
        value. Both are returned rather than one overwriting the other: the
        workflow view carries pause/cancel intent the server view cannot express.
        """

        try:
            client = await self._runtime_client()
            (
                status,
                progress,
                failed,
                quarantined,
                pending,
                execution_status,
            ) = await asyncio.gather(
                client.get_status(run_id),
                client.get_progress(run_id),
                client.get_failed_artifacts(run_id),
                client.get_quarantined_artifacts(run_id),
                client.get_pending_resolutions(run_id),
                client.execution_status(run_id),
            )
            return AppResponse(
                True,
                {
                    "status": to_jsonable_python(status),
                    "execution_status": execution_status,
                    "progress": to_jsonable_python(progress),
                    "failed_artifacts": list(failed),
                    "quarantined_artifacts": list(quarantined),
                    "pending_resolutions": to_jsonable_python(pending),
                },
            )
        except Exception as exc:  # noqa: BLE001 - service returns a stable error envelope
            return self._failure(exc, "read status for ingestion run %r", run_id)

    async def ingestion_result(self, run_id: str) -> AppResponse:
        try:
            result = await (await self._runtime_client()).result(run_id)
            return AppResponse(True, {"result": to_jsonable_python(result)})
        except Exception as exc:  # noqa: BLE001 - service returns a stable error envelope
            return self._failure(exc, "await result for ingestion run %r", run_id)

    async def retrieve(
        self,
        query: str,
        *,
        tenant_id: str,
        top_k: int = 10,
        include_content: bool = False,
    ) -> AppResponse:
        try:
            report = await (await self._retrieval_service()).retrieve(
                query,
                tenant_id=tenant_id,
                top_k=top_k,
            )
            results = []
            for rank, item in enumerate(report.results, start=1):
                result = {
                    "rank": rank,
                    "id": item.id,
                    "score": item.score,
                    "source": item.metadata.get("retrieval_source", "hybrid"),
                }
                if include_content:
                    result["content"] = item.text
                results.append(result)
            return AppResponse(
                True,
                {
                    "request_id": report.request_id,
                    "results": results,
                    "diagnostics": to_jsonable_python(report.diagnostics),
                },
            )
        except Exception as exc:  # noqa: BLE001 - service returns a stable error envelope
            return self._failure(exc, "retrieve for tenant %r", tenant_id)

    async def control_ingestion(
        self,
        run_id: str,
        action: str,
        *,
        artifact_ids: tuple[str, ...] = (),
        graceful: bool = True,
    ) -> AppResponse:
        try:
            client = await self._runtime_client()
            if action == "pause":
                await client.pause(run_id)
            elif action == "resume":
                await client.resume(run_id)
            elif action == "cancel":
                await client.cancel(run_id, graceful=graceful)
            elif action == "retry":
                if not artifact_ids:
                    raise ValueError("retry requires at least one artifact id")
                await client.retry_failed(run_id, artifact_ids)
            else:
                raise ValueError(f"unsupported ingestion action: {action!r}")
            logger.info("Applied %r to ingestion run %r", action, run_id)
            return AppResponse(
                True,
                {
                    "run_id": run_id,
                    "action": action,
                    "artifact_ids": list(artifact_ids),
                },
            )
        except Exception as exc:  # noqa: BLE001 - service returns a stable error envelope
            return self._failure(exc, "apply %r to ingestion run %r", action, run_id)

    async def aclose(self) -> None:
        try:
            if self._retrieval is not None:
                await self._retrieval.aclose()
        finally:
            await self._composition.aclose()

    async def _runtime_client(self) -> TemporalRuntimeClient:
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is None:
                self._client = await self._client_factory(self._runtime_config)
        return self._client

    async def _retrieval_service(self) -> RuntimeRetrievalService:
        if self._retrieval is not None:
            return self._retrieval
        async with self._retrieval_lock:
            if self._retrieval is None:
                self._retrieval = await self._retrieval_factory(self._settings)
        return self._retrieval

    @staticmethod
    def _failure(
        exc: Exception,
        message: str = "Application service call failed",
        *args: object,
    ) -> AppResponse:
        """Return a reviewed message, logging a traceback only when one is needed.

        ``public_error_message`` collapses anything outside its allowlist to a
        bare class name so provider responses and internal storage paths never
        reach a caller. For those the log is the only channel carrying the
        cause, so the traceback is recorded at ERROR.

        When the message is already public the envelope tells the caller
        everything, and a traceback would be pure duplication -- the CLI sets
        ``pretty_exceptions_enable=False`` precisely so handled failures render
        as a panel rather than a stack trace. Those log at DEBUG instead.
        """

        public = public_error_message(exc)
        if public == type(exc).__name__:
            logger.error(message, *args, exc_info=exc)
        else:
            logger.debug(message, *args, exc_info=exc)
        return AppResponse(
            False,
            data={"error_type": type(exc).__name__},
            error=public,
        )
