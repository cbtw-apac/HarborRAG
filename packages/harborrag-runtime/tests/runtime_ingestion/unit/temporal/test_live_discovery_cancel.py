"""A cancel stops the discovery walk quietly, before and after the activity hears of it."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from temporalio.exceptions import ApplicationError, ApplicationErrorCategory
from temporalio.testing import ActivityEnvironment

from harborrag_adapters.connectors.descriptors import (
    ConnectorDocumentDescriptor,
    default_document_descriptor,
)
from harborrag_core.contracts.errors import HarborConflictError
from harborrag_core.domain.source import SourceRecord
from harborrag_core.ingestion import IngestionTaskState
from harborrag_core.ingestion.source_query import ProcessingProfileInput
from harborrag_engine.ingestion.source.tasks import pending_source_task
from harborrag_runtime.ingestion import SourceIngestionService
from harborrag_runtime.ingestion.connections import ConnectionHealth
from harborrag_runtime.temporal.ingestion_activities import IngestionActivities
from harborrag_runtime.temporal.schemas import SourceIngestionInput

from ...fixtures.release import (
    build_control_plane,
    build_release_resources,
    build_release_service,
    source_request,
)

pytestmark = pytest.mark.unit

_TASK = "task-walk"
_ROOTS = 300


class _WalkingConnector:
    """A connector without pages that walks ``_ROOTS`` files, calling ``on_root`` before each."""

    def __init__(self, on_root: Callable[[int], None]) -> None:
        self.walked = 0
        self._on_root = on_root

    def discover(self, query: object) -> Iterator[SourceRecord]:
        del query
        for index in range(_ROOTS):
            self._on_root(index)
            self.walked += 1
            path = f"docs/file-{index:03d}.txt"
            yield SourceRecord(
                id=path,
                source_type="text/plain",
                locator=path,
                checksum=f"sha-{index}",
                metadata={"relative_path": path},
            )

    def describe(self, record: SourceRecord) -> ConnectorDocumentDescriptor:
        return default_document_descriptor(record)


class _NoPlans:
    async def find(self, **kwargs: object) -> None:
        del kwargs


def _source() -> SourceIngestionInput:
    return SourceIngestionInput(
        task_id=_TASK,
        tenant_id="default",
        connector_name="local-docs",
        connector_type="local",
        connection_id="local-docs",
        source_scope_id="docs",
        configuration_fingerprint="local-config-v1",
        processing=ProcessingProfileInput(
            parser_profile="text-v1",
            normalizer_version="canonical-v1",
            chunk_strategy="route-evidence-v3",
            dense_encoder_profile="dense-v1",
            sparse_encoder_profile="bm25-v1",
            graph_projection_version="graph-v1",
        ),
    )


def _activities(
    service: SourceIngestionService, connector_for_run: Callable[..., Awaitable[object]]
) -> IngestionActivities:
    runtime = SimpleNamespace(
        sources=service,
        source_plans=_NoPlans(),
        connector=connector_for_run,
        connection_health=ConnectionHealth(None),
    )
    return IngestionActivities(cast(Any, runtime))


def _environment() -> ActivityEnvironment:
    environment = ActivityEnvironment()
    environment.info = replace(
        environment.info,
        start_to_close_timeout=timedelta(hours=1),
        heartbeat_timeout=timedelta(minutes=2),
    )
    return environment


@pytest.mark.asyncio
async def test_a_scan_cancelled_before_the_activity_hears_of_it_stops_the_walk_quietly(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The workflow's cancel activity closes the scan within a second of a Temporal cancel.

    The discovery attempt only hears of the cancel at its next heartbeat, up to a minute later.
    """
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    loop = asyncio.get_running_loop()
    async with control, resources.store:
        service = SourceIngestionService(
            control=control, documents=build_release_service(resources)
        )

        def cancel_after_the_first_batch(index: int) -> None:
            if index == 60:
                asyncio.run_coroutine_threadsafe(service.cancel(_TASK), loop).result(timeout=30)

        connector = _WalkingConnector(cancel_after_the_first_batch)

        async def connector_for_run(**kwargs: object) -> _WalkingConnector:
            del kwargs
            return connector

        activities = _activities(service, connector_for_run)

        with caplog.at_level(logging.INFO), pytest.raises(ApplicationError) as caught:
            await _environment().run(activities.discover_source_items, _source())
        task = await control.tasks.get(_TASK)

    assert caught.value.category is ApplicationErrorCategory.BENIGN
    assert caught.value.non_retryable
    assert caught.value.type == "source_discovery_cancelled"
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert "immutable_artifact_conflict" not in caplog.text
    assert connector.walked < _ROOTS
    assert task is not None
    assert task.status is IngestionTaskState.CANCELLED


@pytest.mark.asyncio
async def test_a_cancelled_walk_stops_reading_the_source_after_the_item_in_hand(
    tmp_path: Path,
) -> None:
    reached = threading.Event()
    release = threading.Event()

    def hold_at_the_sixth_item(index: int) -> None:
        if index == 5:
            reached.set()
            release.wait(timeout=30)

    connector = _WalkingConnector(hold_at_the_sixth_item)
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    loop = asyncio.get_running_loop()
    async with control, resources.store:
        service = SourceIngestionService(
            control=control, documents=build_release_service(resources)
        )
        walk = asyncio.create_task(
            service.prepare_discovery(source_request(_TASK), cast(Any, connector))
        )
        assert await asyncio.to_thread(reached.wait, 30)

        walk.cancel()
        with pytest.raises(asyncio.CancelledError):
            await walk
        release.set()
        await loop.shutdown_default_executor()

    assert connector.walked == 6


@pytest.mark.asyncio
async def test_a_cancelled_discovery_attempt_stops_quietly_and_the_cancel_closes_its_scan(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A graceful cancel cancels the attempt, waits for it to stop, then records the cancel."""
    reached = threading.Event()
    release = threading.Event()

    def hold_at_the_sixth_item(index: int) -> None:
        if index == 5:
            reached.set()
            release.wait(timeout=30)

    connector = _WalkingConnector(hold_at_the_sixth_item)
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    loop = asyncio.get_running_loop()
    async with control, resources.store:
        service = SourceIngestionService(
            control=control, documents=build_release_service(resources)
        )

        async def connector_for_run(**kwargs: object) -> _WalkingConnector:
            del kwargs
            return connector

        environment = _environment()
        activities = _activities(service, connector_for_run)
        with caplog.at_level(logging.INFO):
            attempt = asyncio.create_task(
                environment.run(activities.discover_source_items, _source())
            )
            assert await asyncio.to_thread(reached.wait, 30)
            environment.cancel()
            with pytest.raises(asyncio.CancelledError):
                await attempt
        release.set()
        await loop.shutdown_default_executor()
        open_after_the_attempt = await control.source_scans.has_running_scan("docs")
        await service.cancel(_TASK)
        open_after_the_cancel = await control.source_scans.has_running_scan("docs")
        task = await control.tasks.get(_TASK)

    assert connector.walked == 6
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert open_after_the_attempt
    assert not open_after_the_cancel
    assert task is not None
    assert task.status is IngestionTaskState.CANCELLED


@pytest.mark.asyncio
async def test_a_conflict_in_a_run_that_is_not_cancelled_still_fails_loudly(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    async with control, resources.store:
        service = SourceIngestionService(
            control=control, documents=build_release_service(resources)
        )
        await control.tasks.create(pending_source_task(source_request(_TASK)))
        await control.tasks.transition(_TASK, IngestionTaskState.RUNNING)

        async def connector_being_deleted(**kwargs: object) -> object:
            del kwargs
            raise HarborConflictError("connection is being deleted")

        activities = _activities(service, connector_being_deleted)

        with caplog.at_level(logging.INFO), pytest.raises(ApplicationError) as caught:
            await _environment().run(activities.discover_source_items, _source())

    assert caught.value.type == "immutable_artifact_conflict"
    assert caught.value.category is ApplicationErrorCategory.UNSPECIFIED
    assert [record.levelno for record in caplog.records if record.levelno >= logging.ERROR]
