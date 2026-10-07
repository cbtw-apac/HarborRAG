"""A publish that lost to a newer version is skipped, never a failed document."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from harborrag_core.ingestion import PublicationSupersededError, SourceAdmissionDecision
from harborrag_runtime.ingestion import DocumentReleaseService
from harborrag_runtime.ingestion.document.pipeline import DocumentStagePipeline
from harborrag_runtime.ingestion.document.stage_models import PreparedDocumentStage

from ...fixtures.connectors import SourceConnector
from ...fixtures.release import (
    build_control_plane,
    build_dependencies,
    build_release_resources,
    release_request,
)


@pytest.mark.asyncio
async def test_a_retried_publish_of_a_version_retired_since_is_skipped(tmp_path: Path) -> None:
    # The first publish of v1 committed and its worker died before acking; v2 then
    # published and retired v1. Temporal's retry of v1's publish used to record the
    # document FAILED with "document version in RETIRED cannot publish".
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    connector = SourceConnector()
    async with control, resources.store:
        dependencies = build_dependencies(resources)
        service = DocumentReleaseService(dependencies)
        await service.provision(tenant_id="default")
        first = await service.release(release_request(source_version="1"), connector)
        connector.body = "The worker timeout is 45 seconds."
        newer = await service.release(release_request(source_version="2"), connector)
        assert first.document_version_id is not None
        assert newer.published is True
        retired = await control.document_versions.get_version(first.document_version_id)
        assert retired is not None

        retried = await DocumentStagePipeline(dependencies).projections.publish_version(
            PreparedDocumentStage(
                document_id=first.document_id,
                document_version_id=first.document_version_id,
                decision=SourceAdmissionDecision.NEW,
                canonical_reference=retired.canonical_artifact,
                expected_active_document_version_id=None,
                publication_base_known=True,
            ),
            tenant_id="default",
        )

        active = await control.document_versions.active_snapshot(first.document_id)
        assert retried.published is False
        assert active is not None
        assert str(active.document_version_id) == newer.document_version_id


@pytest.mark.asyncio
async def test_a_superseded_publish_logs_why_it_was_superseded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    async with control, resources.store:
        service = DocumentReleaseService(build_dependencies(resources))
        await service.provision(tenant_id="default")

        async def removed(**kwargs):  # type: ignore[no-untyped-def]
            raise PublicationSupersededError(
                f"document {kwargs['document_id']} was removed from its source"
            )

        monkeypatch.setattr(control.publisher, "publish", removed)
        with caplog.at_level(logging.INFO, logger="harborrag.runtime.ingestion.projection"):
            outcome = await service.release(release_request(source_version="1"), SourceConnector())

        assert outcome.published is False
        assert "was removed from its source" in caplog.text
