"""Discovery activity with cursor heartbeats and durable source references."""

from __future__ import annotations

import asyncio
import base64
import json
from itertools import islice
from typing import cast

from temporalio import activity

from harborrag_adapters.connectors.schemas import ConnectorPage, ConnectorQuery
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
        page_cursor, page_skip = _decode_runtime_cursor(
            progress.next_cursor
            if progress is not None and progress.next_cursor is not None
            else request.cursor
        )
        remaining = request.page_size - len(references)
        if remaining < 1:
            if progress is None:
                raise ValueError("discovery checkpoint is missing its artifact references")
            return progress
        connector = self._dependencies.connector(request.connector_name)
        query = ConnectorQuery()

        def load_page() -> ConnectorPage:
            discover_page = getattr(connector, "discover_page", None)
            if callable(discover_page):
                return cast(
                    "ConnectorPage",
                    discover_page(
                        query,
                        cursor=page_cursor,
                        page_size=remaining + page_skip,
                    ),
                )
            try:
                offset = int(page_cursor or 0)
            except ValueError as exc:
                raise ValueError("legacy connector received a non-numeric cursor") from exc
            requested = remaining + page_skip
            bounded = ConnectorQuery(limit=offset + requested)
            records = tuple(
                islice(
                    connector.discover(bounded),
                    offset,
                    offset + requested,
                )
            )
            next_cursor = str(offset + len(records)) if len(records) == requested else None
            return ConnectorPage(records, next_cursor)

        page = await asyncio.to_thread(load_page)
        if page_skip > len(page.records):
            raise ValueError("discovery checkpoint exceeds the replayed provider page")
        records = page.records[page_skip:]
        for index, record in enumerate(records, start=1):
            provider_name = getattr(connector, "provider_name", None)
            if isinstance(provider_name, str) and provider_name.strip():
                record.metadata.setdefault("source_kind", provider_name.strip().lower())
            reference = await self._dependencies.state.persist_discovered(request, record)
            references.append(reference)
            progress_cursor = _encode_runtime_cursor(
                page_cursor,
                page_skip + index,
            )
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

        done = page.next_cursor is None
        next_cursor = None if done else _encode_runtime_cursor(page.next_cursor, 0)
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


_CURSOR_PREFIX = "harbor-page-v1:"


def _encode_runtime_cursor(provider_cursor: str | None, skip: int) -> str:
    payload = json.dumps(
        {"provider": provider_cursor, "skip": skip},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _CURSOR_PREFIX + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_runtime_cursor(cursor: str | None) -> tuple[str | None, int]:
    if cursor is None:
        return None, 0
    if not cursor.startswith(_CURSOR_PREFIX):
        return cursor, 0
    encoded = cursor.removeprefix(_CURSOR_PREFIX)
    padding = "=" * (-len(encoded) % 4)
    try:
        value = json.loads(base64.urlsafe_b64decode(encoded + padding))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid runtime discovery cursor") from exc
    provider = value.get("provider")
    skip = value.get("skip")
    if provider is not None and not isinstance(provider, str):
        raise ValueError("invalid runtime discovery cursor")
    if not isinstance(skip, int) or isinstance(skip, bool) or skip < 0:
        raise ValueError("invalid runtime discovery cursor")
    return provider, skip
