"""End-to-end document release pipeline behavior."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from harborrag_adapters.repositories.object_store import MemoryObjectStore
from harborrag_core.ingestion import (
    ChangeFingerprintBuilder,
    ReindexJobState,
    SourceAdmissionDecision,
)
from harborrag_runtime.ingestion import (
    DocumentReindexService,
    DocumentReleaseService,
    ReindexRequest,
)

from ...fixtures.connectors import (
    DeterministicEmbedClient,
    SourceConnector,
    TextParser,
)
from ...fixtures.release import (
    ReleaseResources,
    build_control_plane,
    build_dependencies,
    build_release_service,
    processing_profile,
    release_request,
)
from ...fixtures.storage import (
    InMemoryKnowledgeGraph,
    InMemoryVectorRepository,
)


@pytest.mark.asyncio
async def test_release_publishes_and_unchanged_replay_skips_expensive_stages(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    control = build_control_plane(tmp_path)
    store = MemoryObjectStore()
    connector = SourceConnector()
    parser = TextParser()
    embed = DeterministicEmbedClient()
    vectors = InMemoryVectorRepository()
    graph = InMemoryKnowledgeGraph()
    async with control, store:
        service = build_release_service(
            ReleaseResources(control, store, parser, embed, vectors, graph)
        )
        await service.provision(tenant_id="default")

        with caplog.at_level(
            logging.DEBUG,
            logger="harborrag.runtime.ingestion.document_pipeline",
        ):
            first = await service.release(
                release_request(source_version="1"),
                connector,
            )
        expensive_counts = (connector.loads, parser.calls, len(embed.inputs))
        replay = await service.release(
            release_request(source_version="1"),
            connector,
        )

        assert first.published is True
        assert first.evidence_chunks >= 1
        assert first.document_version_id is not None
        manifest = await control.reliability.projection_manifest(first.document_version_id)
        assert manifest is not None
        assert manifest.comment_artifact is not None
        assert manifest.comment_artifact.key == (
            f"comments/{first.document_id}/{first.document_version_id}.json"
        )
        assert manifest.canonical_comment_ids == ()
        assert manifest.canonical_table_ids == ()
        assert manifest.table_artifacts == ()
        assert replay.decision == SourceAdmissionDecision.UNCHANGED
        assert (connector.loads, parser.calls, len(embed.inputs)) == expensive_counts
        assert "Document stage completed" in caplog.text
        assert "stage=WriteVectorProjection" in caplog.text
        assert "Document pipeline completed" in caplog.text


@pytest.mark.asyncio
async def test_contentless_document_is_skipped_as_unsupported(tmp_path: Path) -> None:
    control = build_control_plane(tmp_path)
    store = MemoryObjectStore()
    connector = SourceConnector()
    connector.body = ""
    parser = TextParser()
    embed = DeterministicEmbedClient()
    vectors = InMemoryVectorRepository()
    graph = InMemoryKnowledgeGraph()
    async with control, store:
        service = build_release_service(
            ReleaseResources(control, store, parser, embed, vectors, graph)
        )

        outcome = await service.release(release_request(source_version="empty-1"), connector)

        assert outcome.decision == SourceAdmissionDecision.UNSUPPORTED
        assert outcome.published is False
        assert outcome.evidence_chunks == 0
        assert embed.inputs == []


@pytest.mark.asyncio
async def test_reindex_rebuilds_from_canonical_without_connector_or_parser_calls(
    tmp_path: Path,
) -> None:
    control = build_control_plane(tmp_path)
    resources = ReleaseResources(
        control=control,
        store=MemoryObjectStore(),
        parser=TextParser(),
        embed=DeterministicEmbedClient(),
        vectors=InMemoryVectorRepository(),
        graph=InMemoryKnowledgeGraph(),
    )
    connector = SourceConnector()
    async with control, resources.store:
        dependencies = build_dependencies(resources)
        documents = DocumentReleaseService(dependencies)
        first = await documents.release(release_request(source_version="1"), connector)
        connector_calls = connector.loads
        parser_calls = resources.parser.calls
        embedding_calls = len(resources.embed.inputs)
        target = processing_profile().model_copy(update={"graph_projection_version": "graph-v2"})

        job = await DocumentReindexService(dependencies).run(
            ReindexRequest(
                reindex_job_id="reindex-1",
                tenant_id="default",
                processing=target,
                document_id=first.document_id,
            )
        )

        active = await control.document_versions.active_snapshot(first.document_id)
        previous = await control.document_versions.get_version(str(first.document_version_id))
        assert job.status == ReindexJobState.COMPLETED
        assert job.connector_call_count == 0
        assert job.scanned_count == 1
        assert job.published_count == 1
        assert connector.loads == connector_calls
        assert resources.parser.calls == parser_calls
        assert len(resources.embed.inputs) == embedding_calls
        assert active is not None
        assert previous is not None
        assert str(active.document_version_id) != first.document_version_id
        assert (
            active.fingerprints.canonical_content_hash
            == previous.fingerprints.canonical_content_hash
        )
        assert (
            active.fingerprints.processing_fingerprint
            == ChangeFingerprintBuilder().processing_fingerprint(profile=target)
        )

        calls_after_reindex = len(resources.embed.inputs)
        replay = await DocumentReindexService(dependencies).run(
            ReindexRequest(
                reindex_job_id="reindex-1",
                tenant_id="default",
                processing=target,
                document_id=first.document_id,
            )
        )
        assert replay == job
        assert len(resources.embed.inputs) == calls_after_reindex
        assert connector.loads == connector_calls


@pytest.mark.asyncio
async def test_failed_reindex_keeps_active_version_and_replays_canonical_boundary(
    tmp_path: Path,
) -> None:
    control = build_control_plane(tmp_path)
    resources = ReleaseResources(
        control=control,
        store=MemoryObjectStore(),
        parser=TextParser(),
        embed=DeterministicEmbedClient(),
        vectors=InMemoryVectorRepository(),
        graph=InMemoryKnowledgeGraph(),
    )
    connector = SourceConnector()
    async with control, resources.store:
        dependencies = build_dependencies(resources)
        first = await DocumentReleaseService(dependencies).release(
            release_request(source_version="1"),
            connector,
        )
        connector_calls = connector.loads
        target = processing_profile().model_copy(update={"graph_projection_version": "graph-v3"})
        request = ReindexRequest(
            reindex_job_id="reindex-retry",
            tenant_id="default",
            processing=target,
            document_id=first.document_id,
        )
        reindex = DocumentReindexService(dependencies)
        resources.graph.fail_writes = True

        failed = await reindex.run(request)

        active = await control.document_versions.active_snapshot(first.document_id)
        assert failed.status == ReindexJobState.FAILED
        assert failed.failure_count == 1
        assert active is not None
        assert str(active.document_version_id) == first.document_version_id
        assert connector.loads == connector_calls

        resources.graph.fail_writes = False
        recovered = await reindex.run(request)
        active = await control.document_versions.active_snapshot(first.document_id)
        assert recovered.status == ReindexJobState.COMPLETED
        assert recovered.published_count == 1
        assert active is not None
        assert str(active.document_version_id) != first.document_version_id
        assert connector.loads == connector_calls


@pytest.mark.asyncio
async def test_metadata_refresh_reencodes_only_route_and_graph_failure_keeps_old_active(
    tmp_path: Path,
) -> None:
    control = build_control_plane(tmp_path)
    store = MemoryObjectStore()
    connector = SourceConnector()
    parser = TextParser()
    embed = DeterministicEmbedClient()
    vectors = InMemoryVectorRepository()
    graph = InMemoryKnowledgeGraph()
    async with control, store:
        service = build_release_service(
            ReleaseResources(control, store, parser, embed, vectors, graph)
        )
        await service.provision(tenant_id="default")
        first = await service.release(release_request(source_version="1"), connector)
        initial_embeddings = len(embed.inputs)

        connector.labels = ["production"]
        metadata = await service.release(
            release_request(
                source_version="1",
                discovery_decision=SourceAdmissionDecision.METADATA_CHANGED,
            ),
            connector,
        )
        assert metadata.decision == SourceAdmissionDecision.METADATA_CHANGED
        assert len(embed.inputs) == initial_embeddings + 1

        connector.body = "A changed worker timeout."
        graph.fail_writes = True
        with pytest.raises(ConnectionError, match="graph unavailable"):
            await service.release(release_request(source_version="2"), connector)

        active = await control.document_versions.active_snapshot(first.document_id)
        cleanup = await control.reliability.pending_cleanup_jobs()
        assert active is not None
        assert str(active.document_version_id) == metadata.document_version_id
        assert any(str(job.document_version_id) != metadata.document_version_id for job in cleanup)


@pytest.mark.asyncio
async def test_failed_release_replays_from_canonical_without_connector_or_parser(
    tmp_path: Path,
) -> None:
    control = build_control_plane(tmp_path)
    resources = ReleaseResources(
        control=control,
        store=MemoryObjectStore(),
        parser=TextParser(),
        embed=DeterministicEmbedClient(),
        vectors=InMemoryVectorRepository(),
        graph=InMemoryKnowledgeGraph(),
    )
    connector = SourceConnector()
    async with control, resources.store:
        dependencies = build_dependencies(resources)
        service = DocumentReleaseService(dependencies)
        first = await service.release(release_request(source_version="1"), connector)
        connector.body = "Replay this changed body from its canonical artifact."
        resources.graph.fail_writes = True

        with pytest.raises(ConnectionError, match="graph unavailable"):
            await service.release(release_request(source_version="2"), connector)

        failed_jobs = await control.reliability.pending_cleanup_jobs()
        failed_version_id = next(
            str(job.document_version_id)
            for job in failed_jobs
            if str(job.document_version_id) != first.document_version_id
        )
        connector_calls = connector.loads
        parser_calls = resources.parser.calls
        resources.graph.fail_writes = False

        replayed = await service.replay(
            release_request(source_version="2"),
            failed_version_id,
        )

        active = await control.document_versions.active_snapshot(first.document_id)
        assert replayed.published is True
        assert replayed.document_version_id == failed_version_id
        assert active is not None
        assert str(active.document_version_id) == failed_version_id
        assert connector.loads == connector_calls
        assert resources.parser.calls == parser_calls
