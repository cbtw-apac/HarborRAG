"""Discovery activity with cursor heartbeats and durable source references."""

from __future__ import annotations

import asyncio
from itertools import islice

from temporalio import activity

from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_core.domain.source import SourceRecord
from harborrag_runtime.temporal.activities.schemas import ActivityTelemetryContext
from harborrag_runtime.temporal.activities.telemetry import record_activity
from harborrag_runtime.temporal.dependencies import RuntimeDependencies
from harborrag_runtime.temporal.schemas import (
    DiscoveryInput,
    DiscoveryResult,
    HeartbeatProgress,
)


class DiscoveryActivities:
    def __init__(self, dependencies: RuntimeDependencies) -> None:
        self._dependencies = dependencies

    @activity.defn(name="harborrag.discover_artifacts")
    async def discover_artifacts(self, request: DiscoveryInput) -> DiscoveryResult:
        """Discover one bounded page and persist every source record idempotently."""

        await self._dependencies.state.initialize_run(request)
        progress = await self._dependencies.state.discovery_progress(request)
        if progress is not None and progress.done:
            return progress
        references = list(progress.artifacts) if progress is not None else []
        offset = int(
            progress.next_cursor
            if progress is not None and progress.next_cursor is not None
            else request.cursor or 0
        )
        remaining = request.page_size - len(references)
        if remaining < 1:
            if progress is None:
                raise ValueError("discovery checkpoint is missing its artifact references")
            return progress
        connector = self._dependencies.connector(request.connector_name)
        query = ConnectorQuery(limit=offset + remaining)

        def load_page() -> tuple[SourceRecord, ...]:
            discovered = connector.discover(query)
            return tuple(islice(discovered, offset, offset + remaining))

        records = await asyncio.to_thread(load_page)
        for index, record in enumerate(records, start=1):
            provider_name = getattr(connector, "provider_name", None)
            if isinstance(provider_name, str) and provider_name.strip():
                record.metadata.setdefault("source_kind", provider_name.strip().lower())
            reference = await self._dependencies.state.persist_discovered(request, record)
            references.append(reference)
            progress_cursor = str(offset + index)
            checkpoint_ref = await self._dependencies.state.save_discovery_progress(
                request,
                tuple(references),
                progress_cursor,
                done=False,
            )
            activity.heartbeat(
                HeartbeatProgress(
                    stage="discovery",
                    completed=len(references),
                    total=request.page_size,
                    cursor=progress_cursor,
                    checkpoint_ref=checkpoint_ref,
                )
            )

        done = len(records) < remaining
        next_cursor = None if done else str(offset + len(records))
        checkpoint_ref = await self._dependencies.state.save_discovery_progress(
            request,
            tuple(references),
            next_cursor,
            done=done,
        )
        activity.heartbeat(
            HeartbeatProgress(
                stage="discovery",
                completed=len(references),
                total=request.page_size,
                cursor=next_cursor,
                checkpoint_ref=checkpoint_ref,
            )
        )
        record_activity(
            self._dependencies,
            "runtime.discovery.completed",
            ActivityTelemetryContext(
                run_id=request.run_id,
                measurements={"discovered": len(references)},
            ),
        )
        return DiscoveryResult(
            artifacts=tuple(references),
            next_cursor=next_cursor,
            checkpoint_ref=checkpoint_ref,
            done=done,
        )
