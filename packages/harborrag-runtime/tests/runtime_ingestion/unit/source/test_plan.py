"""Immutable source plan persistence behavior."""

from __future__ import annotations

import pytest

from harborrag_adapters.repositories.object_store import (
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
    MemoryObjectStore,
)
from harborrag_core.chunking import ConnectorType
from harborrag_core.domain.source import SourceRecord
from harborrag_core.ingestion import (
    AdmissionSnapshot,
    ProcessingProfile,
    SourceIdentity,
)
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_runtime.ingestion.document.models import DocumentReleaseRequest
from harborrag_runtime.ingestion.source.models import (
    PlannedDocumentRelease,
)
from harborrag_runtime.ingestion.source.plan import SourcePlanRepository


def _planned() -> PlannedDocumentRelease:
    return PlannedDocumentRelease(
        document_id="document-1",
        request=DocumentReleaseRequest(
            tenant_id="tenant-1",
            connector_name="local-docs",
            source=SourceRecord(
                id="guide.md",
                source_type="text/markdown",
                locator="file:///docs/guide.md",
                metadata={"title": "Guide", "labels": ["release"]},
            ),
            source_identity=SourceIdentity(
                tenant_id="tenant-1",
                connector_type=ConnectorType.LOCAL,
                connection_id="local-docs",
                source_item_id="guide.md",
                source_scope_id="docs",
            ),
            admission=AdmissionSnapshot(source_version="1"),
            processing=ProcessingProfile(
                parser_profile="parser-v1",
                normalizer_version="canonical-v1",
                chunk_strategy="chunks-v1",
                dense_encoder_profile="dense-v1",
                sparse_encoder_profile="sparse-v1",
                graph_projection_version="graph-v1",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_source_plan_round_trip_is_immutable_and_reference_only() -> None:
    store = MemoryObjectStore()
    context = StorageOperationContext.system(tenant_id="tenant-1")
    async with store:
        repository = SourcePlanRepository(
            ImmutableArtifactWriter(store),
            ImmutableArtifactReader(store),
        )
        missing = await repository.find(
            task_id="task-1",
            scan_id="scan-1",
            context=context,
        )

        first = await repository.put(
            task_id="task-1",
            scan_id="scan-1",
            planned=(_planned(),),
            context=context,
        )
        replay = await repository.put(
            task_id="task-1",
            scan_id="scan-1",
            planned=(_planned(),),
            context=context,
        )
        loaded = await repository.get(first, context=context)
        found = await repository.find(
            task_id="task-1",
            scan_id="scan-1",
            context=context,
        )

    assert missing is None
    assert replay == first
    assert found == first
    assert loaded == (_planned(),)
    assert first.key == "source-plans/task-1/scan-1.json"
