from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from temporalio.testing import ActivityEnvironment

from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.domain.raw_document import RawDocument
from harborrag_runtime.temporal.activities.processing import ProcessingActivities
from harborrag_runtime.temporal.schemas import (
    ArtifactActivityInput,
    ArtifactReference,
    ArtifactStage,
    ArtifactStageState,
)

from .fakes import Dependencies, fetch_request, fetch_state


@pytest.mark.asyncio
async def test_fetch_activity_heartbeats_during_blocking_load(monkeypatch) -> None:
    raw = RawDocument("a", "jira", "hello", "text/markdown")
    state = fetch_state(raw)
    connector = SimpleNamespace(load=Mock(return_value=raw))
    release_load = asyncio.Event()

    async def delayed_to_thread(call, *args):
        await release_load.wait()
        return call(*args)

    monkeypatch.setattr(
        "harborrag_runtime.temporal.activities.processing.asyncio.to_thread",
        delayed_to_thread,
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.activities.processing._heartbeat_interval_seconds",
        lambda: 0.001,
    )
    environment = ActivityEnvironment()
    heartbeats = []

    def record_heartbeat(progress) -> None:
        heartbeats.append(progress)
        if len(heartbeats) >= 2:
            release_load.set()

    environment.on_heartbeat = record_heartbeat
    result = await environment.run(
        ProcessingActivities(Dependencies(connector, state)).fetch_artifact,
        fetch_request(),
    )

    assert len(heartbeats) >= 3
    assert heartbeats[0].completed == 0
    assert heartbeats[1].completed == 0
    assert heartbeats[-1].completed == 1
    assert result.state.stage is ArtifactStage.PARSE
    assert result.state.snapshot_ref == "snapshot://a"


@pytest.mark.asyncio
async def test_fetch_activity_reports_and_propagates_cancellation(
    monkeypatch,
    caplog,
) -> None:
    raw = RawDocument("a", "jira", "hello", "text/markdown")
    state = fetch_state(raw)
    connector = SimpleNamespace(load=Mock(return_value=raw))
    load_started = asyncio.Event()

    async def blocked_to_thread(call, *args):
        load_started.set()
        await asyncio.Event().wait()
        return call(*args)

    monkeypatch.setattr(
        "harborrag_runtime.temporal.activities.processing.asyncio.to_thread",
        blocked_to_thread,
    )
    environment = ActivityEnvironment()
    caplog.set_level(
        logging.INFO,
        logger="harborrag.runtime.temporal.activities.processing",
    )
    running = asyncio.create_task(
        environment.run(
            ProcessingActivities(Dependencies(connector, state)).fetch_artifact,
            fetch_request(),
        )
    )
    await asyncio.wait_for(load_started.wait(), timeout=1)

    environment.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, timeout=1)

    assert "worker_shutdown=False, cancel_requested=True" in caplog.text
    state.persist_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_parse_activity_passes_structured_parser_output_to_normalizer(
    monkeypatch,
) -> None:
    async def direct(call, *args):
        return call(*args)

    monkeypatch.setattr(
        "harborrag_runtime.temporal.activities.processing.asyncio.to_thread",
        direct,
    )
    artifact = ArtifactReference("a", "source://a", "local", "local")
    stage_state = ArtifactStageState(
        artifact=artifact,
        generation_id="generation-1",
        stage=ArtifactStage.PARSE,
        artifact_revision_id="revision-1",
        snapshot_ref="snapshot://a",
    )
    request = ArtifactActivityInput("run-1", "tenant-1", "manifest-1", stage_state)
    raw = RawDocument("a", "local", "hello", "text/plain")
    parsed = ParsedDocument("hello", "text", elements=[])
    document = SimpleNamespace(id="a")
    state = SimpleNamespace(
        completed_stage=AsyncMock(return_value=None),
        load_snapshot=AsyncMock(return_value=raw),
        persist_parsed_document=AsyncMock(return_value="parsed://a"),
        complete_stage=AsyncMock(side_effect=lambda request, result: result),
    )
    parser = SimpleNamespace(parse=Mock(return_value=parsed))
    normalizer = SimpleNamespace(normalize=Mock(return_value=document))
    dependencies = SimpleNamespace(state=state, parser=parser, normalizer=normalizer)

    result = await ActivityEnvironment().run(
        ProcessingActivities(dependencies).parse_artifact,
        request,
    )

    normalizer.normalize.assert_called_once_with(raw, parsed)
    state.persist_parsed_document.assert_awaited_once_with(request, parsed, document)
    assert result.state.parsed_document_ref == "parsed://a"
