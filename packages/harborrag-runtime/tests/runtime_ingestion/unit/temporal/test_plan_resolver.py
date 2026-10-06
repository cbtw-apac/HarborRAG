"""Document lookups read one plan page, cached per worker, never the whole plan."""

from __future__ import annotations

from typing import Any, cast

import pytest
from temporalio.exceptions import ApplicationError

from harborrag_core.ingestion import ArtifactReference
from harborrag_runtime.ingestion.source.models import SourcePlanIndex
from harborrag_runtime.ingestion.source.plan import SourcePlanRepository
from harborrag_runtime.temporal.conversion import to_workflow_artifact
from harborrag_runtime.temporal.plan_resolver import PlanDocumentResolver
from harborrag_runtime.temporal.schemas import DocumentIngestionInput


def _reference(key: str) -> ArtifactReference:
    return ArtifactReference(
        bucket="harborrag-artifacts",
        key=key,
        sha256="0" * 64,
        byte_size=1,
        media_type="application/json",
    )


class FakePlans:
    """Counts every storage read so the test can prove what a lookup costs."""

    plan_identity = staticmethod(SourcePlanRepository.plan_identity)

    def __init__(self, *, pages: dict[int, tuple[str, ...]], indexed: bool) -> None:
        self.pages = pages
        self.indexed = indexed
        self.index_reads = 0
        self.page_reads: list[int] = []
        self.whole_reads = 0

    async def find_index(
        self, *, task_id: str, scan_id: str, context: Any
    ) -> SourcePlanIndex | None:
        self.index_reads += 1
        if not self.indexed:
            return None
        return SourcePlanIndex.from_page_counts([len(self.pages[n]) for n in sorted(self.pages)])

    async def get_page_documents(
        self, *, task_id: str, scan_id: str, page_number: int, context: Any
    ) -> tuple[str, ...] | None:
        self.page_reads.append(page_number)
        return self.pages.get(page_number)

    async def get(self, reference: ArtifactReference, *, context: Any) -> tuple[str, ...]:
        self.whole_reads += 1
        return tuple(doc for n in sorted(self.pages) for doc in self.pages[n])


def _request(index: int, key: str = "source-plans/task-1/scan-1.json") -> DocumentIngestionInput:
    return DocumentIngestionInput(
        task_id="task-1",
        tenant_id="default",
        connector_name="jira",
        plan_reference=to_workflow_artifact(_reference(key)),
        document_index=index,
    )


PAGES = {0: ("d0", "d1", "d2"), 1: ("d3", "d4"), 2: ("d5",)}


@pytest.mark.asyncio
async def test_indexed_plan_reads_only_the_page_that_holds_the_document() -> None:
    plans = FakePlans(pages=PAGES, indexed=True)
    resolver = PlanDocumentResolver(cast(Any, plans))

    assert await resolver.get(_request(4)) == "d4"
    assert await resolver.get(_request(3)) == "d3"  # same page, cached
    assert await resolver.get(_request(5)) == "d5"

    assert plans.index_reads == 1  # once per plan, then cached
    assert plans.page_reads == [1, 2]
    assert plans.whole_reads == 0


@pytest.mark.asyncio
async def test_out_of_range_index_is_rejected_without_reading_pages() -> None:
    plans = FakePlans(pages=PAGES, indexed=True)
    resolver = PlanDocumentResolver(cast(Any, plans))

    with pytest.raises(ApplicationError):
        await resolver.get(_request(6))
    with pytest.raises(ApplicationError):
        await resolver.get(_request(-1))
    assert plans.page_reads == []


@pytest.mark.asyncio
async def test_plan_without_index_falls_back_to_the_whole_plan_once() -> None:
    plans = FakePlans(pages=PAGES, indexed=False)
    resolver = PlanDocumentResolver(cast(Any, plans))

    assert await resolver.get(_request(1)) == "d1"
    assert await resolver.get(_request(5)) == "d5"

    assert plans.index_reads == 1
    assert plans.whole_reads == 1  # cached after the first read
    assert plans.page_reads == []


@pytest.mark.asyncio
async def test_a_reference_that_is_not_a_plan_key_uses_the_whole_plan() -> None:
    plans = FakePlans(pages=PAGES, indexed=True)
    resolver = PlanDocumentResolver(cast(Any, plans))

    assert await resolver.get(_request(2, key="other/artifact.json")) == "d2"
    assert plans.index_reads == 0 and plans.whole_reads == 1


def test_index_locates_pages_and_validates_contiguity() -> None:
    index = SourcePlanIndex.from_page_counts([3, 0, 2])

    assert index.document_count == 5
    assert index.locate(0) == (0, 0)
    assert index.locate(2) == (0, 2)
    assert index.locate(3) == (2, 0)  # the empty page 1 is skipped
    assert index.locate(4) == (2, 1)
    with pytest.raises(IndexError):
        index.locate(5)
    with pytest.raises(ValueError, match="contiguous"):
        SourcePlanIndex(document_count=2, pages=(index.pages[2],))


def test_plan_identity_parses_only_dispatch_plan_keys() -> None:
    assert SourcePlanRepository.plan_identity(_reference("source-plans/t/s.json")) == ("t", "s")
    assert (
        SourcePlanRepository.plan_identity(_reference("source-plans/t/s/pages/00000000.json"))
        is None
    )
    assert SourcePlanRepository.plan_identity(_reference("raw/whatever.json")) is None
