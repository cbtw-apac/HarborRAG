"""Preflight, fetch, and parse activities."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace

from temporalio import activity

from harborrag_runtime.temporal.dependencies import RuntimeDependencies
from harborrag_runtime.temporal.models import (
    ArtifactActivityInput,
    ArtifactActivityResult,
    ArtifactStage,
    ArtifactStatus,
    HeartbeatProgress,
)

logger = logging.getLogger("harborrag.runtime.temporal.activities.processing")

_DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 15.0
_MIN_HEARTBEAT_INTERVAL_SECONDS = 0.01


def _heartbeat_interval_seconds() -> float:
    """Choose an interval safely below the current activity heartbeat timeout."""

    timeout = activity.info().heartbeat_timeout
    if timeout is None:
        return _DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    return max(
        _MIN_HEARTBEAT_INTERVAL_SECONDS,
        min(_DEFAULT_HEARTBEAT_INTERVAL_SECONDS, timeout.total_seconds() / 3),
    )


async def _run_sync_with_heartbeats[ResultT](
    call: Callable[..., ResultT],
    *args: object,
    heartbeat: HeartbeatProgress,
) -> ResultT:
    """Run blocking connector work while keeping its activity lease alive."""

    call_task = asyncio.create_task(asyncio.to_thread(call, *args))
    try:
        while True:
            done, _ = await asyncio.wait(
                (call_task,),
                timeout=_heartbeat_interval_seconds(),
            )
            if call_task in done:
                return call_task.result()
            activity.heartbeat(heartbeat)
    except asyncio.CancelledError:
        # Cancelling the asyncio wrapper cannot interrupt Python code already
        # running in its executor thread, but it prevents a detached task from
        # leaking an unobserved result while the connector's own I/O timeouts
        # bound the underlying call.
        call_task.cancel()
        await asyncio.gather(call_task, return_exceptions=True)
        raise


class ProcessingActivities:
    def __init__(self, dependencies: RuntimeDependencies) -> None:
        self._dependencies = dependencies

    @activity.defn(name="harborrag.preflight_artifact")
    async def preflight_artifact(
        self,
        request: ArtifactActivityInput,
    ) -> ArtifactActivityResult:
        existing = await self._completed(request, ArtifactStage.PREFLIGHT)
        if existing is not None:
            return existing
        activity.heartbeat(HeartbeatProgress(stage="preflight", completed=0, total=1))
        result = await self._dependencies.state.preflight(request)
        activity.heartbeat(HeartbeatProgress(stage="preflight", completed=1, total=1))
        return await self._dependencies.state.complete_stage(request, result)

    @activity.defn(name="harborrag.fetch_artifact")
    async def fetch_artifact(
        self,
        request: ArtifactActivityInput,
    ) -> ArtifactActivityResult:
        existing = await self._completed(request, ArtifactStage.FETCH)
        if existing is not None:
            return existing
        progress = HeartbeatProgress(stage="fetch", completed=0, total=1)
        activity.heartbeat(progress)
        try:
            source = await self._dependencies.state.load_source(request.state.artifact.source_ref)
            connector = self._dependencies.connector(request.state.artifact.connector_name)
            raw = await _run_sync_with_heartbeats(
                connector.load,
                source,
                heartbeat=progress,
            )
            snapshot_ref = await self._dependencies.state.persist_snapshot(request, raw)
            state = replace(
                request.state,
                stage=ArtifactStage.PARSE,
                snapshot_ref=snapshot_ref,
            )
            result = ArtifactActivityResult(status=ArtifactStatus.RUNNING, state=state)
            activity.heartbeat(
                HeartbeatProgress(
                    stage="fetch",
                    completed=1,
                    total=1,
                    checkpoint_ref=snapshot_ref,
                )
            )
            return await self._dependencies.state.complete_stage(request, result)
        except asyncio.CancelledError:
            logger.info(
                "Fetch activity cancelled for artifact %s "
                "(worker_shutdown=%s, cancel_requested=%s)",
                request.state.artifact.artifact_id,
                activity.is_worker_shutdown(),
                activity.is_cancelled(),
            )
            raise

    @activity.defn(name="harborrag.parse_artifact")
    async def parse_artifact(
        self,
        request: ArtifactActivityInput,
    ) -> ArtifactActivityResult:
        existing = await self._completed(request, ArtifactStage.PARSE)
        if existing is not None:
            return existing
        snapshot_ref = request.state.snapshot_ref
        if snapshot_ref is None:
            raise ValueError("parse activity requires snapshot_ref")
        activity.heartbeat(HeartbeatProgress(stage="parse", completed=0, total=1))
        raw = await self._dependencies.state.load_snapshot(snapshot_ref)
        parsed = await asyncio.to_thread(self._dependencies.parser.parse, raw)
        document = await asyncio.to_thread(
            self._dependencies.normalizer.normalize,
            raw,
            parsed,
        )
        parsed_ref = await self._dependencies.state.persist_parsed_document(
            request,
            parsed,
            document,
        )
        state = replace(
            request.state,
            stage=ArtifactStage.CHUNK,
            parsed_document_ref=parsed_ref,
        )
        result = ArtifactActivityResult(status=ArtifactStatus.RUNNING, state=state)
        activity.heartbeat(
            HeartbeatProgress(
                stage="parse",
                completed=1,
                total=1,
                checkpoint_ref=parsed_ref,
            )
        )
        return await self._dependencies.state.complete_stage(request, result)

    async def _completed(
        self,
        request: ArtifactActivityInput,
        stage: ArtifactStage,
    ) -> ArtifactActivityResult | None:
        return await self._dependencies.state.completed_stage(request, stage)
