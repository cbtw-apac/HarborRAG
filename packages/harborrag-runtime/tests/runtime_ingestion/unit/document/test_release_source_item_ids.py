"""Regression: a source item ID that holds spaces is released like any other.

Local and GitHub connectors build source item IDs from file paths, so a file or
folder name with spaces reaches the document pipeline unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_core.chunking import ConnectorType
from harborrag_core.domain.source import SourceRecord
from harborrag_core.ingestion import (
    AdmissionSnapshot,
    SourceAdmissionDecision,
    SourceIdentity,
)
from harborrag_runtime.ingestion import DocumentReleaseRequest

from ...fixtures.connectors import SourceConnector
from ...fixtures.release import (
    build_control_plane,
    build_release_resources,
    build_release_service,
    processing_profile,
)

pytestmark = pytest.mark.unit


def _release_request(connector_type: ConnectorType, source_item_id: str) -> DocumentReleaseRequest:
    return DocumentReleaseRequest(
        tenant_id="default",
        connector_name="docs",
        source=SourceRecord(id=source_item_id, source_type="text/plain", locator=source_item_id),
        source_identity=SourceIdentity(
            tenant_id="default",
            connector_type=connector_type,
            connection_id="docs",
            source_item_id=source_item_id,
            source_scope_id="docs",
        ),
        admission=AdmissionSnapshot(source_version="1"),
        processing=processing_profile(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("connector_type", "source_item_id"),
    [
        (ConnectorType.LOCAL, "guides/Q3 design review.md"),
        (ConnectorType("github"), "github://acme/handbook/docs/Q3 notes.md"),
    ],
)
async def test_release_source_item_id_with_spaces_is_published(
    tmp_path: Path,
    connector_type: ConnectorType,
    source_item_id: str,
) -> None:
    """A document named with spaces is published, and its replay is unchanged."""
    resources = build_release_resources(build_control_plane(tmp_path))
    connector = SourceConnector()
    async with resources.control, resources.store:
        service = build_release_service(resources)
        await service.provision(tenant_id="default")

        first = await service.release(_release_request(connector_type, source_item_id), connector)
        replay = await service.release(_release_request(connector_type, source_item_id), connector)

    assert first.published is True
    assert replay.decision == SourceAdmissionDecision.UNCHANGED
    assert connector.loads == 1
