"""Source ingestion lifecycle behavior."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from harborrag_core.chunking import RelationType
from harborrag_core.ingestion import (
    BindingKind,
    IngestionTaskState,
)
from harborrag_runtime.ingestion import (
    DocumentReleaseService,
    SourceIngestionService,
)
from harborrag_runtime.ingestion.source.tasks import source_scan_id

from ...fixtures.connectors import DescriptorConnector
from ...fixtures.release import (
    build_control_plane,
    build_dependencies,
    build_relation_repair_service,
    build_release_resources,
    build_release_service,
    source_request,
)


@pytest.mark.asyncio
async def test_source_scan_dispatches_root_and_attachment_as_documents(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    connector = DescriptorConnector()
    async with control, resources.store:
        dependencies = build_dependencies(resources)
        document_service = DocumentReleaseService(dependencies)
        await document_service.provision(tenant_id="default")
        service = SourceIngestionService(
            control=control,
            documents=document_service,
            relations=build_relation_repair_service(resources, dependencies),
        )

        with caplog.at_level(logging.INFO, logger="harborrag.runtime.ingestion.source"):
            outcome = await service.ingest(
                source_request("task-1"),
                connector,
            )

        root = await control.source_scans.source_item(
            source_scope_id="docs",
            source_item_id="docs/worker.txt",
        )
        attachment = await control.source_scans.source_item(
            source_scope_id="docs",
            source_item_id="docs/worker.txt/attachments/a1",
        )
        assert outcome.discovered == 2
        assert outcome.published == 2
        assert outcome.failed == 0
        assert outcome.status == IngestionTaskState.COMPLETED
        assert root is not None
        assert root.source_identity.binding.kind == BindingKind.ROOT
        assert attachment is not None
        assert attachment.source_identity.binding.kind == BindingKind.ATTACHMENT
        assert any(
            relation.relation_type == RelationType.HAS_ATTACHMENT
            for relation in resources.graph.relations.values()
        )
        assert "Source ingestion started task_id=task-1" in caplog.text
        assert "Source dispatch completed task_id=task-1 published=2" in caplog.text
        assert "Source ingestion completed task_id=task-1" in caplog.text


@pytest.mark.asyncio
async def test_document_failures_mark_the_source_outcome_failed(
    tmp_path: Path,
) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    resources.graph.fail_writes = True
    async with control, resources.store:
        service = SourceIngestionService(
            control=control,
            documents=build_release_service(resources),
        )

        outcome = await service.ingest(
            source_request("task-document-failure"),
            DescriptorConnector(),
        )

        task = await control.tasks.get("task-document-failure")
        assert outcome.failed == outcome.discovered
        assert outcome.status == IngestionTaskState.FAILED
        assert task is not None
        assert task.status == IngestionTaskState.FAILED


@pytest.mark.asyncio
async def test_mixed_document_results_mark_the_source_outcome_partial(
    tmp_path: Path,
) -> None:
    class FailingAttachmentConnector(DescriptorConnector):
        def load(self, record):
            if record.metadata.get("binding_kind") == "ATTACHMENT":
                raise ValueError("intentional attachment failure")
            return super().load(record)

    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    async with control, resources.store:
        service = SourceIngestionService(
            control=control,
            documents=build_release_service(resources),
        )

        outcome = await service.ingest(
            source_request("task-partial"),
            FailingAttachmentConnector(),
        )

        task = await control.tasks.get("task-partial")
        results = await control.tasks.document_results("task-partial")
        assert outcome.published == 1
        assert outcome.failed == 1
        assert outcome.status == IngestionTaskState.PARTIAL
        assert task is not None
        assert task.status == IngestionTaskState.PARTIAL
        assert {result.status for result in results} == {"published", "failed"}


@pytest.mark.asyncio
async def test_graceful_cancellation_converges_task_state(
    tmp_path: Path,
) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    async with control, resources.store:
        service = SourceIngestionService(
            control=control,
            documents=build_release_service(resources),
        )
        await service.discover(source_request("task-cancel"), DescriptorConnector())

        await service.cancel("task-cancel")
        await service.cancel("task-cancel")

        task = await control.tasks.get("task-cancel")
        assert task is not None
        assert task.status == IngestionTaskState.CANCELLED
        assert task.summary == {"cancelled_at_safe_boundary": True}


@pytest.mark.asyncio
async def test_direct_source_ingestion_persists_terminal_discovery_failure(
    tmp_path: Path,
) -> None:
    class FailingConnector(DescriptorConnector):
        def discover(self, query):
            del query
            raise ConnectionError("provider unavailable")

    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    async with control, resources.store:
        service = SourceIngestionService(
            control=control,
            documents=build_release_service(resources),
        )

        with pytest.raises(ConnectionError, match="provider unavailable"):
            await service.ingest(
                source_request("task-failed-discovery"),
                FailingConnector(),
            )

        task = await control.tasks.get("task-failed-discovery")
        assert task is not None
        assert task.status == IngestionTaskState.FAILED
        assert task.summary == {
            "failed_stage": "discovery",
            "error_code": "discovery_connectionerror",
        }
        next_scan = await control.source_scans.start("docs")
        assert next_scan != source_scan_id("task-failed-discovery")


@pytest.mark.asyncio
async def test_retried_discovery_reuses_its_open_scan(
    tmp_path: Path,
) -> None:
    class FailOnceConnector(DescriptorConnector):
        def __init__(self) -> None:
            super().__init__()
            self.discovery_attempts = 0

        def discover(self, query):
            self.discovery_attempts += 1
            if self.discovery_attempts == 1:
                raise ConnectionError("provider unavailable")
            return super().discover(query)

    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    connector = FailOnceConnector()
    request = source_request("task-retried-discovery")
    async with control, resources.store:
        service = SourceIngestionService(
            control=control,
            documents=build_release_service(resources),
        )

        with pytest.raises(ConnectionError, match="provider unavailable"):
            await service.prepare_discovery(request, connector)
        recovered = await service.prepare_discovery(request, connector)
        await service.complete_discovery(recovered.scan_id)

        assert recovered.scan_id == source_scan_id(request.task_id)
        assert len(recovered.planned) == 2
        assert connector.discovery_attempts == 2


@pytest.mark.asyncio
async def test_finalization_failure_preserves_discovery_and_dispatch_counts(
    tmp_path: Path,
) -> None:
    class FailingRelationRepair:
        async def repair(self, planned, *, tenant_id):
            del planned, tenant_id
            raise RuntimeError("repair unavailable")

    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    async with control, resources.store:
        service = SourceIngestionService(
            control=control,
            documents=build_release_service(resources),
            relations=FailingRelationRepair(),  # type: ignore[arg-type]
        )

        with pytest.raises(RuntimeError, match="repair unavailable"):
            await service.ingest(
                source_request("task-failed-finalization"),
                DescriptorConnector(),
            )

        task = await control.tasks.get("task-failed-finalization")
        assert task is not None
        assert task.status == IngestionTaskState.FAILED
        assert task.summary["failed_stage"] == "relation_repair"
        assert task.summary["discovered"] == 2
        assert task.summary["admitted"] == 2
        assert task.summary["published"] == 2
