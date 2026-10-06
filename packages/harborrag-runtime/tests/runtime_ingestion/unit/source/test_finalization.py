"""Source finalization over a plan it reads one page at a time."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

import pytest

from harborrag_runtime.ingestion.maintenance.relation_repair import RelationRepairResult
from harborrag_runtime.ingestion.source.finalization import SourceFinalizationService
from harborrag_runtime.ingestion.source.models import (
    PlannedDocumentRelease,
    PlannedDocuments,
    RelationRepairProgress,
    SourceDispatchSummary,
)


class _Recorder:
    def __init__(self, **results: object) -> None:
        self.results = results
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def __getattr__(self, name: str) -> Any:
        async def call(*args: object, **kwargs: object) -> object:
            self.calls.append((name, args, kwargs))
            return self.results.get(name)

        return call


class _PageRepair:
    def __init__(self) -> None:
        self.pages: list[tuple[str, ...]] = []

    async def repair(self, planned: Any, *, tenant_id: str) -> RelationRepairResult:
        assert tenant_id == "tenant-1"
        self.pages.append(tuple(item.document_id for item in planned))
        return RelationRepairResult(
            repaired_documents=len(planned), resolved_relations=2, unresolved_relations=1
        )


def _paged_plan(*pages: tuple[str, ...]) -> PlannedDocuments:
    async def read(start: int) -> AsyncIterator[tuple[PlannedDocumentRelease, ...]]:
        for page in pages[start:]:
            yield tuple(
                cast(PlannedDocumentRelease, SimpleNamespace(document_id=document_id))
                for document_id in page
            )

    return PlannedDocuments(document_count=sum(len(page) for page in pages), read_pages=read)


@pytest.mark.asyncio
async def test_finalization_repairs_relations_page_by_page_and_sums_the_results() -> None:
    tasks = _Recorder()
    control = SimpleNamespace(
        tasks=tasks,
        source_scans=_Recorder(reconcile_removals=()),
        publisher=_Recorder(),
    )
    repair = _PageRepair()
    finalization = SourceFinalizationService(
        control=cast(Any, control), relations=cast(Any, repair)
    )
    request = SimpleNamespace(
        task_id="task-1",
        tenant_id="tenant-1",
        missing_threshold=1,
        query=SimpleNamespace(include_attachments=True),
    )

    outcome = await finalization.finish(
        cast(Any, request),
        scan_id="scan-1",
        planned=_paged_plan(("a", "b"), ("c",)),
        summary=SourceDispatchSummary(published=3),
    )

    assert repair.pages == [("a", "b"), ("c",)]
    assert outcome.discovered == 3
    assert outcome.unresolved_relations == 2
    stored = next(kwargs["summary"] for name, _, kwargs in tasks.calls if name == "finalize")
    assert cast(dict[str, object], stored)["discovered"] == 3
    assert cast(dict[str, object], stored)["unresolved_relations"] == 2


@pytest.mark.asyncio
async def test_finalization_resumes_relation_repair_after_the_pages_already_done() -> None:
    # A retry must not repeat hours of repair: pages a prior attempt finished are
    # skipped, and their totals still count toward the outcome.
    control = SimpleNamespace(
        tasks=_Recorder(),
        source_scans=_Recorder(reconcile_removals=()),
        publisher=_Recorder(),
    )
    repair = _PageRepair()
    finalization = SourceFinalizationService(
        control=cast(Any, control), relations=cast(Any, repair)
    )
    request = SimpleNamespace(
        task_id="task-1",
        tenant_id="tenant-1",
        missing_threshold=1,
        query=SimpleNamespace(include_attachments=True),
    )
    progress = RelationRepairProgress(
        next_page=1, repaired_documents=2, resolved_relations=2, unresolved_relations=1
    )

    outcome = await finalization.finish(
        cast(Any, request),
        scan_id="scan-1",
        planned=_paged_plan(("a", "b"), ("c",)),
        summary=SourceDispatchSummary(published=3),
        repair_progress=progress,
    )

    assert repair.pages == [("c",)]
    assert outcome.unresolved_relations == 2
    assert progress == RelationRepairProgress(
        next_page=2, repaired_documents=3, resolved_relations=4, unresolved_relations=2
    )


def test_repair_progress_resumes_only_from_a_well_formed_heartbeat() -> None:
    assert RelationRepairProgress.resume(
        {
            "next_page": 4,
            "repaired_documents": 9,
            "resolved_relations": 3,
            "unresolved_relations": 1,
        }
    ) == RelationRepairProgress(4, 9, 3, 1)
    # Earlier attempts heartbeated a plain string; anything malformed starts over.
    assert RelationRepairProgress.resume("finalize-source") == RelationRepairProgress()
    assert RelationRepairProgress.resume({"next_page": -1}) == RelationRepairProgress()
