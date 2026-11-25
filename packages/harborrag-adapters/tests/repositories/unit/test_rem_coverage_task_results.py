"""Per-document task outcomes count by status, read back singly and page by keyset."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from harborrag_adapters.repositories.database.ingestion_control import (
    IngestionControlPlaneDatabase,
)
from harborrag_core.ingestion import IngestionTask, IngestionTaskState, TaskDocumentResult
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId

from .ingestion_control_fixtures import make_control_plane

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _result(task_id: str, document_id: str, status: str) -> TaskDocumentResult:
    return TaskDocumentResult(
        task_id=task_id,
        document_id=DocumentId(document_id),
        document_version_id=DocumentVersionId(f"{document_id}-v1") if status != "SKIPPED" else None,
        status=status,
        result={"safe_error_code": "graph_unavailable"} if status == "FAILED" else {},
    )


@pytest_asyncio.fixture
async def control(tmp_path: Path) -> AsyncIterator[IngestionControlPlaneDatabase]:
    async with make_control_plane(tmp_path) as control_plane:
        for task_id in ("task-1", "task-2"):
            await control_plane.tasks.create(
                IngestionTask(
                    task_id=task_id,
                    source_scope_id="scope-engineering",
                    status=IngestionTaskState.PENDING,
                    request={"connector": "confluence"},
                )
            )
        for document_id, status in (
            ("document-a", "PUBLISHED"),
            ("document-b", "PUBLISHED"),
            ("document-c", "FAILED"),
            ("document-d", "SKIPPED"),
        ):
            await control_plane.tasks.record_document_result(_result("task-1", document_id, status))
        yield control_plane


async def test_progress_counts_each_status_of_one_task(
    control: IngestionControlPlaneDatabase,
) -> None:
    assert await control.tasks.progress("task-1") == {"published": 2, "failed": 1, "skipped": 1}
    assert await control.tasks.progress("task-2") == {}


async def test_progress_for_tasks_groups_by_task_and_leaves_out_empty_ones(
    control: IngestionControlPlaneDatabase,
) -> None:
    counts = await control.tasks.progress_for_tasks(["task-1", "task-2", "task-1"])

    assert counts == {"task-1": {"published": 2, "failed": 1, "skipped": 1}}
    assert await control.tasks.progress_for_tasks([]) == {}


async def test_document_result_reads_one_outcome(control: IngestionControlPlaneDatabase) -> None:
    failed = await control.tasks.document_result("task-1", "document-c")
    skipped = await control.tasks.document_result("task-1", "document-d")

    assert failed == _result("task-1", "document-c", "FAILED")
    assert skipped is not None
    assert skipped.document_version_id is None
    assert await control.tasks.document_result("task-2", "document-c") is None


@pytest.mark.parametrize(("task_id", "document_id"), [(" ", "document-a"), ("task-1", "")])
async def test_document_reads_reject_blank_ids(
    control: IngestionControlPlaneDatabase, task_id: str, document_id: str
) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        await control.tasks.document_result(task_id, document_id)
    with pytest.raises(ValueError, match="non-empty"):
        await control.tasks.document_results(" ")
    with pytest.raises(ValueError, match="non-empty"):
        await control.tasks.document_results_page(" ")


async def test_page_walks_the_outcomes_by_keyset(control: IngestionControlPlaneDatabase) -> None:
    first = await control.tasks.document_results_page("task-1", limit=3)
    last = first.items[-1]
    second = await control.tasks.document_results_page(
        "task-1", after_updated_at=last.updated_at, after_document_id=last.document_id, limit=3
    )

    assert first.has_more is True
    assert second.has_more is False
    assert [item.document_id for item in (*first.items, *second.items)] == [
        "document-a",
        "document-b",
        "document-c",
        "document-d",
    ]


async def test_page_filters_by_status_case_insensitively(
    control: IngestionControlPlaneDatabase,
) -> None:
    published = await control.tasks.document_results_page("task-1", statuses=["published"])
    none = await control.tasks.document_results_page("task-1", statuses=[])

    assert [item.document_id for item in published.items] == ["document-a", "document-b"]
    assert none.items == ()
    assert none.has_more is False


@pytest.mark.parametrize(
    "arguments",
    [{"limit": 0}, {"limit": 201}, {"after_document_id": "document-a"}],
)
async def test_page_rejects_a_bad_limit_or_half_a_cursor(
    control: IngestionControlPlaneDatabase, arguments: dict[str, object]
) -> None:
    with pytest.raises(ValueError, match="document result"):
        await control.tasks.document_results_page("task-1", **arguments)
