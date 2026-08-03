"""Durable document-version lifecycle behavior."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from harborrag_core.ingestion import (
    CleanupJobState,
    SourceAdmissionDecision,
)
from harborrag_core.storage import StorageOperationContext
from harborrag_runtime.ingestion import (
    DocumentReleaseService,
    ProjectionCleanupService,
)

from ...fixtures.connectors import SourceConnector
from ...fixtures.release import (
    build_control_plane,
    build_dependencies,
    build_release_resources,
    release_request,
)


@pytest.mark.asyncio
async def test_retired_version_replay_cancels_cleanup_and_republishes(
    tmp_path: Path,
) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    connector = SourceConnector()
    async with control, resources.store:
        service = DocumentReleaseService(build_dependencies(resources))
        await service.provision(tenant_id="default")
        first = await service.release(release_request(source_version="1"), connector)

        connector.labels = ["production"]
        second = await service.release(
            release_request(
                source_version="1",
                discovery_decision=SourceAdmissionDecision.METADATA_CHANGED,
            ),
            connector,
        )
        first_cleanup = await control.reliability.cleanup_for_version(
            str(first.document_version_id)
        )
        assert first_cleanup is not None
        assert first_cleanup.status == CleanupJobState.PENDING

        connector.labels = ["operations"]
        replay = await service.release(
            release_request(
                source_version="1",
                discovery_decision=SourceAdmissionDecision.METADATA_CHANGED,
            ),
            connector,
        )

        first_cleanup = await control.reliability.cleanup_for_version(
            str(first.document_version_id)
        )
        assert replay.document_version_id == first.document_version_id
        assert replay.published is True
        assert first_cleanup is not None
        assert first_cleanup.status == CleanupJobState.CANCELLED
        assert second.document_version_id != replay.document_version_id


@pytest.mark.asyncio
async def test_cleanup_removes_retired_vector_and_graph_projections(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    connector = SourceConnector()
    async with control, resources.store:
        dependencies = build_dependencies(resources)
        service = DocumentReleaseService(dependencies)
        await service.provision(tenant_id="default")
        first = await service.release(release_request(source_version="1"), connector)
        first_manifest = await control.reliability.projection_manifest(
            str(first.document_version_id)
        )
        assert first_manifest is not None

        connector.labels = ["production"]
        await service.release(
            release_request(
                source_version="1",
                discovery_decision=SourceAdmissionDecision.METADATA_CHANGED,
            ),
            connector,
        )
        with caplog.at_level(logging.INFO, logger="harborrag.runtime.ingestion.cleanup"):
            result = await ProjectionCleanupService(
                control=control,
                vector_store=dependencies.vector_store,
                graph_store=resources.graph,
            ).run_documents(
                tenant_id="default",
                document_ids=(first.document_id,),
            )

        remaining_point_ids = {
            point_id for collection in resources.vectors.points.values() for point_id in collection
        }
        retired_point_ids = {
            *first_manifest.route_point_ids,
            *first_manifest.evidence_point_ids,
        }
        job = await control.reliability.cleanup_for_version(str(first.document_version_id))
        assert result.completed == 1
        assert not (remaining_point_ids & retired_point_ids)
        assert all(
            str(node.document_version_id) != first.document_version_id
            for node in resources.graph.nodes.values()
        )
        assert job is not None
        assert job.status == CleanupJobState.COMPLETED
        assert "Projection cleanup completed" in caplog.text
        assert "Projection cleanup batch completed jobs=1 claimed=1 completed=1" in caplog.text


@pytest.mark.asyncio
async def test_metadata_update_encodes_fresh_when_previous_chunks_are_missing(
    tmp_path: Path,
) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    connector = SourceConnector()
    async with control, resources.store:
        service = DocumentReleaseService(build_dependencies(resources))
        await service.provision(tenant_id="default")
        first = await service.release(release_request(source_version="1"), connector)
        active = await control.document_versions.active_snapshot(first.document_id)
        assert active is not None
        assert active.chunk_artifact is not None
        await resources.store.delete(
            active.chunk_artifact.bucket,
            active.chunk_artifact.key,
            context=StorageOperationContext.system("default"),
        )

        connector.labels = ["metadata-refresh"]
        updated = await service.release(
            release_request(
                source_version="1",
                discovery_decision=SourceAdmissionDecision.METADATA_CHANGED,
            ),
            connector,
        )

        assert updated.published is True
        assert updated.document_version_id != first.document_version_id
