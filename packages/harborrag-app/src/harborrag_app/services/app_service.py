"""Application use cases backed by the HarborRAG Temporal runtime client."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from uuid import uuid4

from pydantic_core import to_jsonable_python

from harborrag_app.services.base import AppResponse, BaseAppService
from harborrag_runtime.composition import CompositionRoot
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.temporal.client import TemporalRuntimeClient
from harborrag_runtime.temporal.schemas import IngestionRunInput

type ClientFactory = Callable[
    [TemporalRuntimeConfig],
    Awaitable[TemporalRuntimeClient],
]


class AppService(BaseAppService):
    """Keep transport concerns outside the canonical Temporal ingestion path."""

    def __init__(
        self,
        composition: CompositionRoot,
        settings: RuntimeSettings | None = None,
        *,
        client_factory: ClientFactory = TemporalRuntimeClient.connect,
    ) -> None:
        self._composition = composition
        self._settings = settings or RuntimeSettings()
        self._runtime_config = TemporalRuntimeConfig.from_settings(self._settings)
        self._client_factory = client_factory
        self._client: TemporalRuntimeClient | None = None
        self._client_lock = asyncio.Lock()

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
            return self._failure(exc)

    async def start_ingestion(
        self,
        *,
        tenant_id: str,
        connector_name: str,
        run_id: str | None = None,
        manifest_id: str | None = None,
        generation_id: str | None = None,
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
            options=self._runtime_config.workflow_options(),
        )
        try:
            client = await self._runtime_client()
            reference = await client.start_ingestion(request)
            data = {
                "run": to_jsonable_python(request),
                "workflow": to_jsonable_python(reference),
            }
            if wait:
                data["result"] = to_jsonable_python(await client.result(run_id))
            return AppResponse(True, data)
        except Exception as exc:  # noqa: BLE001 - service returns a stable error envelope
            return self._failure(exc)

    async def ingestion_status(self, run_id: str) -> AppResponse:
        try:
            client = await self._runtime_client()
            status, progress, failed, quarantined, pending = await asyncio.gather(
                client.get_status(run_id),
                client.get_progress(run_id),
                client.get_failed_artifacts(run_id),
                client.get_quarantined_artifacts(run_id),
                client.get_pending_resolutions(run_id),
            )
            return AppResponse(
                True,
                {
                    "status": to_jsonable_python(status),
                    "progress": to_jsonable_python(progress),
                    "failed_artifacts": list(failed),
                    "quarantined_artifacts": list(quarantined),
                    "pending_resolutions": to_jsonable_python(pending),
                },
            )
        except Exception as exc:  # noqa: BLE001 - service returns a stable error envelope
            return self._failure(exc)

    async def ingestion_result(self, run_id: str) -> AppResponse:
        try:
            result = await (await self._runtime_client()).result(run_id)
            return AppResponse(True, {"result": to_jsonable_python(result)})
        except Exception as exc:  # noqa: BLE001 - service returns a stable error envelope
            return self._failure(exc)

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
            return AppResponse(
                True,
                {
                    "run_id": run_id,
                    "action": action,
                    "artifact_ids": list(artifact_ids),
                },
            )
        except Exception as exc:  # noqa: BLE001 - service returns a stable error envelope
            return self._failure(exc)

    async def aclose(self) -> None:
        await self._composition.aclose()

    async def _runtime_client(self) -> TemporalRuntimeClient:
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is None:
                self._client = await self._client_factory(self._runtime_config)
        return self._client

    @staticmethod
    def _failure(exc: Exception) -> AppResponse:
        return AppResponse(
            False,
            data={"error_type": type(exc).__name__},
            error=str(exc) or type(exc).__name__,
        )
