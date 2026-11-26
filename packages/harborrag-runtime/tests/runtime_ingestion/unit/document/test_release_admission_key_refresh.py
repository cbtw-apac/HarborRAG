"""A refetch that finds nothing new stops the next run from fetching the item again."""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord
from harborrag_core.ingestion import SourceAdmissionDecision

from ...fixtures.connectors import SourceConnector
from ...fixtures.release import (
    build_control_plane,
    build_release_resources,
    build_release_service,
    release_request,
)

pytestmark = pytest.mark.unit


class _SteadyConnector(SourceConnector):
    """A file whose bytes and metadata stay the same on every load."""

    def load(self, record: SourceRecord) -> RawDocument:
        raw = super().load(record)
        return RawDocument(
            id=raw.id,
            source=raw.source,
            content=raw.content,
            content_type=raw.content_type,
            metadata={**raw.metadata, "checksum": "source-steady"},
        )


@pytest.mark.asyncio
async def test_the_run_after_an_unchanged_refetch_decides_unchanged_before_fetching(
    tmp_path: Path,
) -> None:
    resources = build_release_resources(build_control_plane(tmp_path))
    connector = _SteadyConnector()
    async with resources.control, resources.store:
        service = build_release_service(resources)
        await service.provision(tenant_id="default")

        first = await service.release(release_request(source_version="1"), connector)
        refetched = await service.release(release_request(source_version="2"), connector)
        loads_after_refetch = connector.loads
        again = await service.release(release_request(source_version="2"), connector)

    assert first.published is True
    assert refetched.decision == SourceAdmissionDecision.UNCHANGED
    assert loads_after_refetch == 2
    assert again.decision == SourceAdmissionDecision.UNCHANGED
    assert again.document_version_id == first.document_version_id
    assert connector.loads == 2
