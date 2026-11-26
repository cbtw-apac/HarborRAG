"""A version stored before payload fields were hashed is refreshed without re-embedding."""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.repositories.object_store import MemoryObjectStore
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord
from harborrag_core.ingestion import SourceAdmissionDecision
from harborrag_engine.ingestion import admission

from ...fixtures.connectors import DeterministicEmbedClient, SourceConnector, TextParser
from ...fixtures.release import (
    ReleaseResources,
    build_control_plane,
    build_release_service,
    release_request,
)
from ...fixtures.storage import InMemoryKnowledgeGraph, InMemoryVectorRepository

pytestmark = pytest.mark.unit


class _DatedConnector(SourceConnector):
    """A local file that reports its modification time."""

    def load(self, record: SourceRecord) -> RawDocument:
        raw = super().load(record)
        return RawDocument(
            id=raw.id,
            source=raw.source,
            content=raw.content,
            content_type=raw.content_type,
            metadata={**raw.metadata, "updated_at": "2026-01-09T00:00:00+00:00"},
        )


@pytest.mark.asyncio
async def test_a_version_hashed_before_payload_fields_is_metadata_changed_and_reuses_vectors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = ReleaseResources(
        control=build_control_plane(tmp_path),
        store=MemoryObjectStore(),
        parser=TextParser(),
        embed=DeterministicEmbedClient(),
        vectors=InMemoryVectorRepository(),
        graph=InMemoryKnowledgeGraph(),
    )
    connector = _DatedConnector()
    async with resources.control, resources.store:
        service = build_release_service(resources)
        await service.provision(tenant_id="default")
        with monkeypatch.context() as earlier_release:
            earlier_release.setattr(
                admission, "_projected_source_fields", lambda document, source_identity: {}
            )
            first = await service.release(release_request(source_version="1"), connector)
        first_inputs = list(resources.embed.inputs)

        upgraded = await service.release(release_request(source_version="2"), connector)

        upgrade_inputs = resources.embed.inputs[len(first_inputs) :]
        previous = await resources.control.document_versions.get_version(
            str(first.document_version_id)
        )
        active = await resources.control.document_versions.active_snapshot(first.document_id)
        assert first.published is True
        assert upgraded.decision == SourceAdmissionDecision.METADATA_CHANGED
        assert upgraded.published is True
        assert previous is not None and active is not None
        assert str(active.document_version_id) == upgraded.document_version_id
        assert (
            active.fingerprints.canonical_content_hash
            == previous.fingerprints.canonical_content_hash
        )
        route_inputs = [text for text in first_inputs if text.startswith("Title: ")]
        assert len(first_inputs) == 2
        assert len(route_inputs) == 1
        assert upgrade_inputs == route_inputs
