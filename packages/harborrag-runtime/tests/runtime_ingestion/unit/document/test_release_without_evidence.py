"""A source item that stops yielding evidence leaves no searchable version behind."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from harborrag_adapters.repositories.object_store import MemoryObjectStore
from harborrag_core.ingestion import DocumentIngestionOutcome, SourceAdmissionDecision
from harborrag_engine.ingestion.release.models import DocumentReleaseOutcome
from harborrag_runtime.ingestion.source.documents import SourceDocumentService

from ...fixtures.connectors import DeterministicEmbedClient, SourceConnector, TextParser
from ...fixtures.release import ReleaseResources, build_control_plane, build_release_service
from ...fixtures.release import release_request as _release_request
from ...fixtures.storage import InMemoryKnowledgeGraph, InMemoryVectorRepository
from ..source.test_plan import _planned

pytestmark = pytest.mark.unit


def _resources(tmp_path: Path) -> ReleaseResources:
    return ReleaseResources(
        control=build_control_plane(tmp_path),
        store=MemoryObjectStore(),
        parser=TextParser(),
        embed=DeterministicEmbedClient(),
        vectors=InMemoryVectorRepository(),
        graph=InMemoryKnowledgeGraph(),
    )


def _empty(connector: SourceConnector) -> None:
    connector.body = ""
    connector.title = None


@pytest.mark.asyncio
async def test_an_emptied_document_retires_its_active_version(tmp_path: Path) -> None:
    resources = _resources(tmp_path)
    connector = SourceConnector()
    async with resources.control, resources.store:
        service = build_release_service(resources)
        await service.provision(tenant_id="default")
        first = await service.release(_release_request(source_version="1"), connector)
        assert first.published is True
        embeddings = len(resources.embed.inputs)

        _empty(connector)
        emptied = await service.release(_release_request(source_version="2"), connector)

        active = await resources.control.document_versions.active_snapshot(first.document_id)
        cleanup = await resources.control.reliability.pending_cleanup_jobs(tenant_id="default")
        assert emptied.decision == SourceAdmissionDecision.REMOVED_CANDIDATE
        assert emptied.published is False
        assert emptied.document_version_id == first.document_version_id
        assert active is None
        assert [str(job.document_version_id) for job in cleanup] == [first.document_version_id]
        assert len(resources.embed.inputs) == embeddings

        again = await service.release(_release_request(source_version="3"), connector)
        assert again.decision == SourceAdmissionDecision.UNSUPPORTED


@pytest.mark.asyncio
async def test_an_empty_document_without_an_active_version_writes_nothing(
    tmp_path: Path,
) -> None:
    resources = _resources(tmp_path)
    connector = SourceConnector()
    _empty(connector)
    async with resources.control, resources.store:
        service = build_release_service(resources)

        outcome = await service.release(_release_request(source_version="1"), connector)

        active = await resources.control.document_versions.active_snapshot(outcome.document_id)
        cleanup = await resources.control.reliability.pending_cleanup_jobs(tenant_id="default")
        assert outcome.decision == SourceAdmissionDecision.UNSUPPORTED
        assert outcome.published is False
        assert active is None
        assert cleanup == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "stored_status", "skip_reason"),
    (
        (SourceAdmissionDecision.REMOVED_CANDIDATE, "skipped", "no_indexable_content"),
        (SourceAdmissionDecision.UNSUPPORTED, "unchanged", None),
    ),
)
async def test_a_retired_document_is_recorded_as_skipped(
    decision: SourceAdmissionDecision,
    stored_status: str,
    skip_reason: str | None,
) -> None:
    recorded = AsyncMock()
    control = SimpleNamespace(tasks=SimpleNamespace(record_document_result=recorded))
    service = SourceDocumentService(control=cast(Any, control), documents=cast(Any, object()))

    outcome = await service.record_published_document(
        "task-1",
        _planned(),
        DocumentReleaseOutcome(
            document_id="document-1",
            document_version_id="version-1",
            decision=decision,
        ),
    )

    result = recorded.await_args.args[0]
    assert outcome == DocumentIngestionOutcome.UNCHANGED
    assert result.status == stored_status
    assert result.result.get("skip_reason") == skip_reason
    assert result.result["decision"] == decision.value
