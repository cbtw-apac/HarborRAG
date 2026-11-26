"""A cancelled run keeps every skip it counted, at discovery and per document."""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_core.ingestion import IngestionTaskState
from harborrag_core.ingestion.connector_contracts import ConnectorSkip
from harborrag_engine.ingestion.source.models import DISCOVERY_SKIPS_KEY
from harborrag_runtime.ingestion import SourceIngestionService

from ...fixtures.connectors import DescriptorConnector
from ...fixtures.release import (
    build_control_plane,
    build_release_resources,
    build_release_service,
    source_request,
)

pytestmark = pytest.mark.unit


class _SkippingConnector(DescriptorConnector):
    """A connector whose discovery left one file out for its extension."""

    @property
    def skipped(self) -> tuple[ConnectorSkip, ...]:
        """The file the extension filters dropped."""
        return (
            ConnectorSkip(
                path="notes.txt",
                reason="unsupported_extension",
                detail="extension '.txt' is not allowed",
            ),
        )


@pytest.mark.asyncio
async def test_a_cancelled_run_stores_its_discovery_and_document_skips(tmp_path: Path) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    async with control, resources.store:
        service = SourceIngestionService(
            control=control, documents=build_release_service(resources)
        )
        discovery = await service.discover(source_request("task-cancel"), _SkippingConnector())
        root, attachment = discovery.planned
        await service.record_failed_document(
            "task-cancel",
            root,
            error_type="parser_rejected_document",
            failed_stage="ParseAndNormalize",
        )
        await service.record_failed_document(
            "task-cancel", attachment, error_type="itemskipped", skip_reason="DELETED"
        )

        await service.cancel("task-cancel")
        task = await control.tasks.get("task-cancel")

    assert task is not None
    assert task.status is IngestionTaskState.CANCELLED
    assert task.summary["skipped"] == 3
    assert task.summary["skipped_by_reason"] == {
        "DELETED": 1,
        "EXTRACTION_FAILED": 1,
        "unsupported_extension": 1,
    }
    assert task.summary[DISCOVERY_SKIPS_KEY] == {"unsupported_extension": 1}
    assert task.summary["cancelled_at_safe_boundary"] is True
