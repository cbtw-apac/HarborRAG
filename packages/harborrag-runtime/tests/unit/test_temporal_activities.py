from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from harborrag_adapters.connectors.exceptions import AuthenticationError
from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord
from harborrag_engine.ingestion.indexing.schemas import IndexingStatus
from harborrag_runtime.temporal.activities.chunking import ChunkingActivities
from harborrag_runtime.temporal.activities.discovery import DiscoveryActivities
from harborrag_runtime.temporal.activities.indexing import IndexingActivities
from harborrag_runtime.temporal.activities.processing import ProcessingActivities
from harborrag_runtime.temporal.models import (
    ArtifactActivityInput,
    ArtifactActivityResult,
    ArtifactReference,
    ArtifactStage,
    ArtifactStageState,
    ArtifactStatus,
    DiscoveryInput,
    DiscoveryResult,
)
from temporalio.testing import ActivityEnvironment


class _Observer:
    def record(self, event, attributes) -> None:
        return None


class _Connector:
    def __init__(
        self,
        records=(),
        error: Exception | None = None,
        provider_name: str | None = None,
    ) -> None:
        self.records = tuple(records)
        self.error = error
        self.provider_name = provider_name

    def discover(self, query):
        if self.error is not None:
            raise self.error
        return iter(self.records)


class _DiscoveryState:
    def __init__(self) -> None:
        self.initialize_run = AsyncMock()
        self.discovery_progress = AsyncMock(return_value=None)
        self.save_discovery_progress = AsyncMock(return_value="checkpoint://discovery/2")

    async def persist_discovered(self, request, source):
        return ArtifactReference(
            artifact_id=source.id,
            source_ref=f"source://{source.id}",
            source_kind=source.metadata.get("source_kind", "local"),
            connector_name=request.connector_name,
            checksum=source.checksum,
        )


class _Dependencies:
    def __init__(self, connector, state) -> None:
        self._connector = connector
        self.state = state
        self.observer = _Observer()

    def connector(self, name):
        return self._connector


def _fetch_request() -> ArtifactActivityInput:
    artifact = ArtifactReference("a", "source://a", "jira", "jira")
    return ArtifactActivityInput(
        "run-1",
        "tenant-1",
        "manifest-1",
        ArtifactStageState(
            artifact=artifact,
            generation_id="generation-1",
            stage=ArtifactStage.FETCH,
            artifact_revision_id="revision-1",
        ),
    )


def _fetch_state(raw: RawDocument) -> SimpleNamespace:
    return SimpleNamespace(
        completed_stage=AsyncMock(return_value=None),
        load_source=AsyncMock(
            return_value=SourceRecord(
                id="a",
                source_type="application/vnd.atlassian.jira.issue+json",
                locator="ENG-1",
            )
        ),
        persist_snapshot=AsyncMock(return_value="snapshot://a"),
        complete_stage=AsyncMock(side_effect=lambda request, result: result),
    )


@pytest.mark.asyncio
async def test_discovery_returns_references_and_heartbeats(monkeypatch) -> None:
    async def direct(call, *args):
        return call(*args)

    monkeypatch.setattr(
        "harborrag_runtime.temporal.activities.discovery.asyncio.to_thread",
        direct,
    )
    records = (
        SourceRecord(id="a", source_type="text/plain", locator="a"),
        SourceRecord(id="b", source_type="text/plain", locator="b"),
    )
    state = _DiscoveryState()
    activities = DiscoveryActivities(_Dependencies(_Connector(records), state))
    environment = ActivityEnvironment()
    heartbeats = []
    environment.on_heartbeat = heartbeats.append

    result = await environment.run(
        activities.discover_artifacts,
        DiscoveryInput(
            run_id="run-1",
            tenant_id="tenant-1",
            manifest_id="manifest-1",
            connector_name="local",
            cursor=None,
            page_size=10,
        ),
    )

    assert tuple(item.artifact_id for item in result.artifacts) == ("a", "b")
    assert result.done is True
    assert result.checkpoint_ref == "checkpoint://discovery/2"
    assert heartbeats[-1].checkpoint_ref == result.checkpoint_ref
    state.initialize_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_discovery_uses_connector_provider_for_source_kind(monkeypatch) -> None:
    async def direct(call, *args):
        return call(*args)

    monkeypatch.setattr(
        "harborrag_runtime.temporal.activities.discovery.asyncio.to_thread",
        direct,
    )
    record = SourceRecord(
        id="jira://ENG/ENG-1",
        source_type="application/vnd.atlassian.jira.issue+json",
        locator="ENG-1",
    )
    state = _DiscoveryState()
    activities = DiscoveryActivities(
        _Dependencies(_Connector((record,), provider_name="jira"), state)
    )

    result = await ActivityEnvironment().run(
        activities.discover_artifacts,
        DiscoveryInput(
            run_id="run-1",
            tenant_id="tenant-1",
            manifest_id="manifest-1",
            connector_name="team-jira",
            cursor=None,
            page_size=10,
        ),
    )

    assert result.artifacts[0].source_kind == "jira"
    assert record.metadata["source_kind"] == "jira"


@pytest.mark.asyncio
async def test_discovery_preserves_non_retryable_adapter_error(monkeypatch) -> None:
    async def direct(call, *args):
        return call(*args)

    monkeypatch.setattr(
        "harborrag_runtime.temporal.activities.discovery.asyncio.to_thread",
        direct,
    )
    activities = DiscoveryActivities(
        _Dependencies(
            _Connector(error=AuthenticationError("invalid credential")), _DiscoveryState()
        )
    )

    with pytest.raises(AuthenticationError):
        await ActivityEnvironment().run(
            activities.discover_artifacts,
            DiscoveryInput(
                run_id="run-1",
                tenant_id="tenant-1",
                manifest_id="manifest-1",
                connector_name="local",
                cursor=None,
                page_size=10,
            ),
        )


@pytest.mark.asyncio
async def test_discovery_resume_keeps_references_persisted_before_retry(
    monkeypatch,
) -> None:
    async def direct(call, *args):
        return call(*args)

    monkeypatch.setattr(
        "harborrag_runtime.temporal.activities.discovery.asyncio.to_thread",
        direct,
    )
    records = (
        SourceRecord(id="a", source_type="text/plain", locator="a"),
        SourceRecord(id="b", source_type="text/plain", locator="b"),
    )
    request = DiscoveryInput(
        run_id="run-1",
        tenant_id="tenant-1",
        manifest_id="manifest-1",
        connector_name="local",
        cursor=None,
        page_size=10,
    )
    state = _DiscoveryState()
    state.discovery_progress.return_value = DiscoveryResult(
        artifacts=(ArtifactReference("a", "source://a", "local", "local"),),
        next_cursor="1",
        checkpoint_ref="checkpoint://partial",
        done=False,
    )

    result = await ActivityEnvironment().run(
        DiscoveryActivities(_Dependencies(_Connector(records), state)).discover_artifacts,
        request,
    )

    assert tuple(item.artifact_id for item in result.artifacts) == ("a", "b")
    assert result.done is True


@pytest.mark.asyncio
async def test_fetch_activity_heartbeats_during_blocking_load(monkeypatch) -> None:
    raw = RawDocument("a", "jira", "hello", "text/markdown")
    state = _fetch_state(raw)
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
        ProcessingActivities(_Dependencies(connector, state)).fetch_artifact,
        _fetch_request(),
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
    state = _fetch_state(raw)
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
            ProcessingActivities(_Dependencies(connector, state)).fetch_artifact,
            _fetch_request(),
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


@pytest.mark.asyncio
async def test_chunk_activity_calls_engine_and_reuses_completed_stage() -> None:
    artifact = ArtifactReference("a", "source://a", "local", "local")
    stage_state = ArtifactStageState(
        artifact=artifact,
        generation_id="generation-1",
        stage=ArtifactStage.CHUNK,
        artifact_revision_id="revision-1",
        parsed_document_ref="parsed://a",
    )
    request = ArtifactActivityInput("run-1", "tenant-1", "manifest-1", stage_state)
    completed = ArtifactActivityResult(
        status=ArtifactStatus.RUNNING,
        state=stage_state,
    )
    state = SimpleNamespace(completed_stage=AsyncMock(return_value=completed))
    chunker = SimpleNamespace(chunk=Mock())
    dependencies = SimpleNamespace(state=state, chunker=chunker)

    result = await ActivityEnvironment().run(
        ChunkingActivities(dependencies).chunk_artifact,
        request,
    )

    assert result is completed
    chunker.chunk.assert_not_called()


@pytest.mark.asyncio
async def test_chunk_activity_delegates_to_engine_and_persists_before_reference(
    monkeypatch,
) -> None:
    async def direct(call, *args):
        return call(*args)

    monkeypatch.setattr(
        "harborrag_runtime.temporal.activities.chunking.asyncio.to_thread",
        direct,
    )
    artifact = ArtifactReference("a", "source://a", "local", "local")
    stage_state = ArtifactStageState(
        artifact=artifact,
        generation_id="generation-1",
        stage=ArtifactStage.CHUNK,
        artifact_revision_id="revision-1",
        parsed_document_ref="parsed://a",
    )
    request = ArtifactActivityInput("run-1", "tenant-1", "manifest-1", stage_state)
    engine_result = SimpleNamespace(manifest=SimpleNamespace(total_chunk_count=3))
    document = SimpleNamespace(
        id="a",
        content_type="text/plain",
        provenance=SimpleNamespace(source="local", extra={}),
    )
    state = SimpleNamespace(
        completed_stage=AsyncMock(return_value=None),
        load_parsed_document=AsyncMock(return_value=document),
        persist_chunking_result=AsyncMock(return_value="chunks://a"),
        complete_stage=AsyncMock(side_effect=lambda request, result: result),
    )
    chunker = SimpleNamespace(chunk=Mock(return_value=engine_result))
    persistence = SimpleNamespace(persist=AsyncMock())
    dependencies = SimpleNamespace(
        state=state,
        chunker=chunker,
        chunk_persistence=persistence,
        observer=_Observer(),
    )

    result = await ActivityEnvironment().run(
        ChunkingActivities(dependencies).chunk_artifact,
        request,
    )

    chunker.chunk.assert_called_once()
    persistence.persist.assert_awaited_once_with(engine_result)
    state.persist_chunking_result.assert_awaited_once_with(request, engine_result)
    assert result.state.chunking_result_ref == "chunks://a"


@pytest.mark.asyncio
async def test_failed_engine_index_result_is_persisted_then_retried() -> None:
    artifact = ArtifactReference("a", "source://a", "local", "local")
    stage_state = ArtifactStageState(
        artifact=artifact,
        generation_id="generation-1",
        stage=ArtifactStage.INDEX,
        artifact_revision_id="revision-1",
        chunking_result_ref="chunks://a",
    )
    request = ArtifactActivityInput("run-1", "tenant-1", "manifest-1", stage_state)
    engine_result = SimpleNamespace(
        status=IndexingStatus.FAILED,
        validation_errors=("vector provider unavailable",),
    )
    state = SimpleNamespace(
        completed_stage=AsyncMock(return_value=None),
        load_chunking_result=AsyncMock(return_value=SimpleNamespace()),
        indexing_request=AsyncMock(return_value=SimpleNamespace()),
        persist_indexing_result=AsyncMock(return_value="index://failed"),
    )
    dependencies = SimpleNamespace(
        state=state,
        indexer=SimpleNamespace(index=AsyncMock(return_value=engine_result)),
    )

    with pytest.raises(RuntimeError, match="persisted result reference"):
        await ActivityEnvironment().run(
            IndexingActivities(dependencies).index_artifact,
            request,
        )

    state.persist_indexing_result.assert_awaited_once_with(request, engine_result)
