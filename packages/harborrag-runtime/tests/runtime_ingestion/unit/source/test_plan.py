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
    SourcePlanCheckpoint,
    SourcePlanIndex,
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


@pytest.mark.asyncio
async def test_plan_index_and_pages_locate_documents_without_the_whole_plan() -> None:
    store = MemoryObjectStore()
    context = StorageOperationContext.system(tenant_id="tenant-1")
    planned = tuple(_planned() for _ in range(5))
    async with store:
        repository = SourcePlanRepository(
            ImmutableArtifactWriter(store),
            ImmutableArtifactReader(store),
        )
        assert (
            await repository.find_index(task_id="task-2", scan_id="scan-2", context=context) is None
        )

        reference = await repository.put_pages_and_index(
            task_id="task-2", scan_id="scan-2", planned=planned, context=context, page_size=2
        )
        index = await repository.find_index(task_id="task-2", scan_id="scan-2", context=context)
        assert index is not None
        page_number, offset = index.locate(4)
        page = await repository.get_page_documents(
            task_id="task-2", scan_id="scan-2", page_number=page_number, context=context
        )
        missing = await repository.get_page_documents(
            task_id="task-2", scan_id="scan-2", page_number=9, context=context
        )

    assert [p.count for p in index.pages] == [2, 2, 1]
    assert reference.key == "source-plans/task-2/scan-2/index.json"
    assert SourcePlanRepository.plan_identity(reference) == ("task-2", "scan-2")
    assert (page_number, offset) == (2, 0)
    assert page is not None and page[offset] == planned[4]
    assert missing is None


def _planned_named(document_id: str) -> PlannedDocumentRelease:
    return PlannedDocumentRelease(document_id=document_id, request=_planned().request)


@pytest.mark.asyncio
async def test_a_paged_plan_is_found_by_its_index_and_read_page_by_page() -> None:
    store = MemoryObjectStore()
    context = StorageOperationContext.system(tenant_id="tenant-1")
    planned = tuple(_planned_named(f"document-{n}") for n in range(5))
    async with store:
        repository = SourcePlanRepository(
            ImmutableArtifactWriter(store),
            ImmutableArtifactReader(store),
        )
        # A whole plan from an older run of the same scan does not shadow the index.
        await repository.put(task_id="task-3", scan_id="scan-3", planned=planned, context=context)
        written = await repository.put_pages_and_index(
            task_id="task-3", scan_id="scan-3", planned=planned, context=context, page_size=2
        )
        found = await repository.find(task_id="task-3", scan_id="scan-3", context=context)
        assert found == written
        documents = await repository.documents(written, context=context)
        pages = [page async for page in documents.pages()]

    assert documents.document_count == 5
    assert [len(page) for page in pages] == [2, 2, 1]
    assert tuple(item for page in pages for item in page) == planned


@pytest.mark.asyncio
async def test_a_whole_plan_from_an_older_run_is_still_readable_as_documents() -> None:
    store = MemoryObjectStore()
    context = StorageOperationContext.system(tenant_id="tenant-1")
    planned = (_planned_named("document-a"), _planned_named("document-b"))
    async with store:
        repository = SourcePlanRepository(
            ImmutableArtifactWriter(store),
            ImmutableArtifactReader(store),
        )
        whole = await repository.put(
            task_id="task-4", scan_id="scan-4", planned=planned, context=context
        )
        assert await repository.find(task_id="task-4", scan_id="scan-4", context=context) == whole
        documents = await repository.documents(whole, context=context)
        pages = [page async for page in documents.pages()]

    assert documents.document_count == 2
    assert pages == [planned]


@pytest.mark.asyncio
async def test_reading_a_plan_whose_page_is_missing_fails_instead_of_skipping_it() -> None:
    store = MemoryObjectStore()
    context = StorageOperationContext.system(tenant_id="tenant-1")
    async with store:
        repository = SourcePlanRepository(
            ImmutableArtifactWriter(store),
            ImmutableArtifactReader(store),
        )
        # An index naming two pages when only the first was persisted.
        await repository.put_page(
            task_id="task-5",
            scan_id="scan-5",
            page_number=0,
            checkpoint=SourcePlanCheckpoint(
                planned=(_planned_named("document-0"),), next_cursor="next", root_count=1
            ),
            context=context,
        )
        reference = await repository.put_index(
            task_id="task-5",
            scan_id="scan-5",
            index=SourcePlanIndex.from_page_counts((1, 1)),
            context=context,
        )
        documents = await repository.documents(reference, context=context)
        with pytest.raises(ValueError, match="page is missing"):
            [page async for page in documents.pages()]
