"""Projection repair reuses the active canonical document and representations."""

from pathlib import Path

import pytest

from harborrag_adapters.repositories.object_store import MemoryObjectStore
from harborrag_core.ingestion import SourceAdmissionDecision

from ...fixtures.connectors import DeterministicEmbedClient, SourceConnector, TextParser
from ...fixtures.release import (
    ReleaseResources,
    build_control_plane,
    build_release_service,
    release_request,
)
from ...fixtures.storage import InMemoryKnowledgeGraph, InMemoryVectorRepository


@pytest.mark.asyncio
async def test_force_reprocess_restores_missing_active_vector_projection(
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
        first_version = first.document_version_id
        embedded = len(embed.inputs)
        vectors.points["evidence"].clear()

        restored = await service.release(
            release_request(source_version="1", force_reprocess=True), connector
        )

        active = await control.document_versions.active_snapshot(first.document_id)
        assert restored.published is True
        assert restored.decision == SourceAdmissionDecision.FORCE_REPROCESS
        assert restored.document_version_id == first_version
        assert restored.evidence_chunks >= 1
        assert vectors.points["evidence"]
        assert len(embed.inputs) == embedded
        assert active is not None and str(active.document_version_id) == first_version
