"""Source discovery activity replay behavior."""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any, cast

import pytest
from temporalio.exceptions import ApplicationError

from harborrag_adapters.connectors.descriptors import ConnectorDocumentDescriptor
from harborrag_adapters.connectors.schemas import ConnectorCapabilities, ConnectorPage
from harborrag_adapters.repositories.object_store import (
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
)
from harborrag_core.domain.source import SourceRecord
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_runtime.ingestion import SourceIngestionService, SourcePlanRepository
from harborrag_runtime.temporal.ingestion_activities import (
    IngestionActivities,
)
from harborrag_runtime.temporal.schemas import (
    ProcessingProfileInput,
    SourceIngestionInput,
)

from ...fixtures.connectors import DescriptorConnector
from ...fixtures.release import (
    build_control_plane,
    build_release_resources,
    build_release_service,
    processing_profile,
    source_request,
)


class CountingDescriptorConnector(DescriptorConnector):
    def __init__(self) -> None:
        super().__init__()
        self.discovery_calls = 0

    def discover(self, query):
        self.discovery_calls += 1
        return super().discover(query)


class CheckpointedDescriptorConnector(DescriptorConnector):
    capabilities = ConnectorCapabilities(pagination=True, concurrent_describe=True)

    def __init__(self) -> None:
        super().__init__()
        self.page_cursors: list[str | None] = []
        self.failed_second_page = False
        self.active_descriptors = 0
        self.max_active_descriptors = 0
        self._descriptor_lock = threading.Lock()

    def discover(self, query):
        raise AssertionError("native pagination must not call discover")

    def discover_page(self, query, *, cursor, page_size):
        del query, page_size
        self.page_cursors.append(cursor)
        if cursor is None:
            return ConnectorPage(
                (self._record("one.txt"), self._record("two.txt")),
                "page-2",
            )
        if cursor == "page-2" and not self.failed_second_page:
            self.failed_second_page = True
            raise OSError("temporary provider failure")
        return ConnectorPage((self._record("three.txt"),), None)

    def describe(self, record: SourceRecord) -> ConnectorDocumentDescriptor:
        with self._descriptor_lock:
            self.active_descriptors += 1
            self.max_active_descriptors = max(
                self.max_active_descriptors,
                self.active_descriptors,
            )
        try:
            time.sleep(0.02)
            return super().describe(record)
        finally:
            with self._descriptor_lock:
                self.active_descriptors -= 1

    @staticmethod
    def _record(name: str) -> SourceRecord:
        return SourceRecord(
            id=f"docs/{name}",
            source_type="text/plain",
            locator=f"file:///docs/{name}",
            metadata={"relative_path": f"docs/{name}"},
        )


@pytest.mark.asyncio
async def test_discovery_replay_uses_plan_written_before_scan_completion(
    tmp_path,
) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    connector = CountingDescriptorConnector()
    request = source_request("task-plan-replay")
    context = StorageOperationContext.system(tenant_id="default")
    writer = ImmutableArtifactWriter(resources.store)
    reader = ImmutableArtifactReader(resources.store)
    plans = SourcePlanRepository(writer, reader)
    async with control, resources.store:
        sources = SourceIngestionService(
            control=control,
            documents=build_release_service(resources),
        )
        prepared = await asyncio.wait_for(
            sources.prepare_discovery(request, connector),
            timeout=5,
        )
        reference = await asyncio.wait_for(
            plans.put(
                task_id=request.task_id,
                scan_id=prepared.scan_id,
                planned=prepared.planned,
                context=context,
            ),
            timeout=5,
        )
        runtime = SimpleNamespace(
            sources=sources,
            source_plans=plans,
            connector=lambda _, **__: connector,
        )
        activities = IngestionActivities(cast(Any, runtime))

        first_replay = await asyncio.wait_for(
            activities.discover_source_items(_source_input()),
            timeout=5,
        )
        completed_replay = await asyncio.wait_for(
            activities.discover_source_items(_source_input()),
            timeout=5,
        )

        assert first_replay.plan_reference.key == reference.key
        assert completed_replay == first_replay
        assert first_replay.document_count == 2
        assert connector.discovery_calls == 1
        assert (
            await control.source_scans.reconcile_removals(
                prepared.scan_id,
            )
            == ()
        )


@pytest.mark.asyncio
async def test_native_discovery_resumes_from_immutable_page_checkpoint(tmp_path) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    connector = CheckpointedDescriptorConnector()
    writer = ImmutableArtifactWriter(resources.store)
    reader = ImmutableArtifactReader(resources.store)
    plans = SourcePlanRepository(writer, reader)
    async with control, resources.store:
        sources = SourceIngestionService(
            control=control,
            documents=build_release_service(resources),
        )
        runtime = SimpleNamespace(
            sources=sources,
            source_plans=plans,
            connector=lambda _, **__: connector,
        )
        activities = IngestionActivities(cast(Any, runtime))
        source = _source_input(
            task_id="task-page-replay",
            discovery_page_size=2,
            discovery_concurrency=2,
        )

        with pytest.raises(ApplicationError):
            await activities.discover_source_items(source)
        result = await activities.discover_source_items(source)

        assert result.document_count == 6
        assert connector.page_cursors == [None, "page-2", "page-2"]
        assert connector.max_active_descriptors == 2
        checkpoint = await plans.find_page(
            task_id=source.task_id,
            scan_id=result.scan_id,
            page_number=0,
            context=StorageOperationContext.system(source.tenant_id),
        )
        assert checkpoint is not None


def _source_input(
    *,
    task_id: str = "task-plan-replay",
    discovery_page_size: int = 50,
    discovery_concurrency: int = 4,
) -> SourceIngestionInput:
    processing = processing_profile()
    return SourceIngestionInput(
        task_id=task_id,
        tenant_id="default",
        connector_name="local-docs",
        connector_type="local",
        connection_id="local-docs",
        source_scope_id="docs",
        configuration_fingerprint="local-config-v1",
        discovery_page_size=discovery_page_size,
        discovery_concurrency=discovery_concurrency,
        processing=ProcessingProfileInput(
            parser_profile=processing.parser_profile,
            normalizer_version=processing.normalizer_version,
            chunk_strategy=processing.chunk_strategy,
            dense_encoder_profile=processing.dense_encoder_profile,
            sparse_encoder_profile=processing.sparse_encoder_profile,
            graph_projection_version=processing.graph_projection_version,
        ),
    )
