"""Cross-document graph relation repair behavior."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from harborrag_core.chunking import RelationType
from harborrag_core.ingestion import DocumentIdentityBuilder, KnowledgeNodeKind
from harborrag_core.storage import StorageOperationContext
from harborrag_runtime.ingestion import (
    DocumentReindexService,
    DocumentReleaseService,
    ProjectionCleanupService,
    ReindexRequest,
    SourceIngestionService,
)

from ...fixtures.connectors import LinkedDocumentsConnector
from ...fixtures.release import (
    ReleaseResources,
    build_control_plane,
    build_dependencies,
    build_relation_repair_service,
    build_release_resources,
    processing_profile,
    source_request,
)


@pytest.mark.asyncio
async def test_relation_repair_bounds_document_concurrency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    connector = LinkedDocumentsConnector()
    async with control, resources.store:
        dependencies = build_dependencies(resources)
        repair = build_relation_repair_service(
            resources,
            dependencies,
            max_concurrency=2,
        )
        discovery = await SourceIngestionService(
            control=control,
            documents=DocumentReleaseService(dependencies),
            relations=repair,
        ).discover(source_request("task-bounded-repair"), connector)
        target = discovery.planned[0]
        planned = tuple(replace(target, document_id=f"document-{index}") for index in range(6))
        active = 0
        maximum_active = 0

        async def observe(*args, **kwargs) -> tuple[int, int, int]:
            nonlocal active, maximum_active
            del args, kwargs
            active += 1
            maximum_active = max(maximum_active, active)
            try:
                await asyncio.sleep(0)
                return (0, 0, 0)
            finally:
                active -= 1

        monkeypatch.setattr(repair, "_repair_one", observe)

        await repair.repair(planned, tenant_id="default")

        assert maximum_active == 2


@pytest.mark.asyncio
async def test_source_batch_repairs_links_after_all_roots_publish(
    tmp_path: Path,
) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    connector = LinkedDocumentsConnector()
    async with control, resources.store:
        dependencies = build_dependencies(resources)
        documents = DocumentReleaseService(dependencies)
        repair = build_relation_repair_service(resources, dependencies)
        source_service = SourceIngestionService(
            control=control,
            documents=documents,
            relations=repair,
        )

        outcome = await source_service.ingest(
            source_request("task-links"),
            connector,
        )

        assert outcome.published == 2
        assert outcome.unresolved_relations == 0
        links = _links(resources)
        assert len(links) == 1
        repaired_nodes, repaired_relations = resources.graph.write_batches[-1]
        assert repaired_relations
        assert all(relation.source_explicit for relation in repaired_relations)
        assert all(node.node_kind == KnowledgeNodeKind.DOCUMENT for node in repaired_nodes)


@pytest.mark.asyncio
async def test_relation_repair_does_not_mutate_a_newer_active_version(
    tmp_path: Path,
) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    connector = LinkedDocumentsConnector()
    async with control, resources.store:
        dependencies = build_dependencies(resources)
        documents = DocumentReleaseService(dependencies)
        repair = build_relation_repair_service(resources, dependencies)
        source_service = SourceIngestionService(
            control=control,
            documents=documents,
            relations=repair,
        )
        newer_profile = processing_profile().model_copy(
            update={"graph_projection_version": "graph-v2"},
        )
        await source_service.ingest(
            replace(
                source_request("task-current"),
                processing=newer_profile,
            ),
            connector,
        )
        stale_discovery = await source_service.discover(
            source_request("task-stale"),
            connector,
        )
        batches_before = len(resources.graph.write_batches)

        result = await repair.repair(stale_discovery.planned, tenant_id="default")

        assert result.repaired_documents == 0
        assert result.resolved_relations == 0
        assert len(resources.graph.write_batches) == batches_before


@pytest.mark.asyncio
async def test_relation_repair_degrades_when_an_active_artifact_was_cleaned(
    tmp_path: Path,
) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    connector = LinkedDocumentsConnector()
    async with control, resources.store:
        dependencies = build_dependencies(resources)
        documents = DocumentReleaseService(dependencies)
        repair = build_relation_repair_service(resources, dependencies)
        discovery = await SourceIngestionService(
            control=control,
            documents=documents,
        ).discover(source_request("task-stale-artifact"), connector)
        for item in discovery.planned:
            await documents.release(item.request, connector)
        stale = await control.document_versions.active_snapshot(discovery.planned[0].document_id)
        assert stale is not None and stale.canonical_artifact is not None
        await resources.store.delete(
            stale.canonical_artifact.bucket,
            stale.canonical_artifact.key,
            context=StorageOperationContext.system("default"),
        )

        result = await repair.repair(
            discovery.planned,
            tenant_id="default",
        )

        assert result.repaired_documents == 0
        assert result.unresolved_relations == 1


@pytest.mark.asyncio
async def test_sequential_reindex_repairs_links_after_target_cleanup(
    tmp_path: Path,
) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    connector = LinkedDocumentsConnector()
    async with control, resources.store:
        dependencies = build_dependencies(resources)
        documents = DocumentReleaseService(dependencies)
        repair = build_relation_repair_service(resources, dependencies)
        await SourceIngestionService(
            control=control,
            documents=documents,
            relations=repair,
        ).ingest(source_request("task-links"), connector)
        source_a = await control.source_scans.source_item(
            source_scope_id="docs",
            source_item_id="docs/a.txt",
        )
        source_b = await control.source_scans.source_item(
            source_scope_id="docs",
            source_item_id="docs/b.txt",
        )
        assert source_a is not None and source_a.document_id is not None
        assert source_b is not None and source_b.document_id is not None
        target = processing_profile().model_copy(
            update={"graph_projection_version": "graph-v2"},
        )
        reindex = DocumentReindexService(dependencies)
        cleanup = ProjectionCleanupService(
            control=control,
            vector_store=dependencies.vector_store,
            graph_store=resources.graph,
        )
        document_ids = (str(source_a.document_id), str(source_b.document_id))
        for sequence, document_id in enumerate(document_ids, start=1):
            await reindex.run(
                ReindexRequest(
                    reindex_job_id=f"reindex-{sequence}",
                    tenant_id="default",
                    processing=target,
                    document_id=document_id,
                )
            )
            await cleanup.run_documents(
                tenant_id="default",
                document_ids=(document_id,),
            )

        assert not _links(resources)

        result = await repair.repair_reindexed(
            tenant_id="default",
            processing=target,
            anchor_document_id=str(source_b.document_id),
        )

        active = await control.document_versions.active_versions(document_ids)
        identities = DocumentIdentityBuilder()
        expected_endpoints = {
            identities.node_key(
                node_kind=KnowledgeNodeKind.DOCUMENT,
                logical_id=document_id,
                document_version_id=version.document_version_id,
            )
            for document_id, version in active.items()
        }
        links = _links(resources)
        assert result.resolved_relations == 1
        assert len(links) == 1
        assert {
            links[0].source_node_key,
            links[0].target_node_key,
        } == expected_endpoints


@pytest.mark.asyncio
async def test_link_target_reindex_does_not_change_source_version_artifacts(
    tmp_path: Path,
) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    connector = LinkedDocumentsConnector()
    async with control, resources.store:
        dependencies = build_dependencies(resources)
        documents = DocumentReleaseService(dependencies)
        repair = build_relation_repair_service(resources, dependencies)
        source_service = SourceIngestionService(
            control=control,
            documents=documents,
            relations=repair,
        )
        discovery = await source_service.discover(
            source_request("task-stable-relations"),
            connector,
        )
        source, target = discovery.planned
        await documents.release(target.request, connector)
        first_source = await documents.release(source.request, connector)
        target_profile = processing_profile().model_copy(
            update={"graph_projection_version": "graph-new-target"},
        )
        await DocumentReindexService(dependencies).run(
            ReindexRequest(
                reindex_job_id="reindex-link-target",
                tenant_id="default",
                processing=target_profile,
                document_id=target.document_id,
            )
        )
        await ProjectionCleanupService(
            control=control,
            vector_store=dependencies.vector_store,
            graph_store=resources.graph,
        ).run_documents(
            tenant_id="default",
            document_ids=(target.document_id,),
        )

        replayed_source = await documents.release(
            replace(source.request, force_reprocess=True),
            connector,
        )
        repaired = await repair.repair(
            discovery.planned,
            tenant_id="default",
        )

        assert replayed_source.document_version_id == first_source.document_version_id
        assert repaired.resolved_relations == 1
        assert len(_links(resources)) == 1


def _links(resources: ReleaseResources):
    return [
        relation
        for relation in resources.graph.relations.values()
        if relation.relation_type == RelationType.LINKS_TO
    ]
