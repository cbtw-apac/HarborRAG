"""Durable paging behavior behind GET /v1/ingestions."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_app.workflow_control.errors import IngestionCursorError
from harborrag_app.workflow_control.ingestion.task_pages import TaskListingMixin
from harborrag_core.ingestion import (
    IngestionTask,
    IngestionTaskState,
    TaskDocumentResult,
)
from harborrag_core.schemas.ids import DocumentId
from harborrag_runtime.temporal.task_registry import IngestionTaskRegistry


class TaskListingService(TaskListingMixin):
    """The listing use case alone, with no Temporal client to compose."""

    def __init__(self, registry: IngestionTaskRegistry) -> None:
        async def provider() -> IngestionTaskRegistry:
            return registry

        self._task_store_provider = provider


@pytest_asyncio.fixture
async def listing(
    tmp_path: Path,
) -> AsyncIterator[tuple[TaskListingService, IngestionControlPlaneDatabase]]:
    client = SQLAlchemyDBClient(
        backend="sqlite",
        url=f"sqlite+aiosqlite:///{tmp_path / 'ingestion-list.db'}",
        pool_size=None,
        max_overflow=None,
        pool_recycle_seconds=1_800,
        echo=False,
    )
    control = IngestionControlPlaneDatabase(client, create_schema=True)
    await control.connect()
    try:
        yield TaskListingService(IngestionTaskRegistry(control)), control
    finally:
        await control.close()


async def _seed_task(
    control: IngestionControlPlaneDatabase,
    task_id: str,
    *,
    tenant: str = "DEFAULT",
) -> None:
    await control.tasks.register(
        IngestionTask(
            task_id=task_id,
            source_scope_id="scope-1",
            status=IngestionTaskState.PENDING,
            request={"tenant_id": tenant, "connector_type": "local", "connection_id": "c1"},
        )
    )


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_pages_walk_every_task_newest_first(
    listing: tuple[TaskListingService, IngestionControlPlaneDatabase],
) -> None:
    """A cursor walk visits each task exactly once, most recent first."""
    service, control = listing
    for index in range(1, 6):
        await _seed_task(control, f"task-{index}")

    visited: list[str] = []
    cursor: str | None = None
    while True:
        page = await service.list_tasks(tenants=None, status=None, cursor=cursor, limit=2)
        visited.extend(str(item["task_id"]) for item in page["items"])  # type: ignore[index]
        cursor = page["next_cursor"]  # type: ignore[assignment]
        if cursor is None:
            break

    assert visited == ["task-5", "task-4", "task-3", "task-2", "task-1"]


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_last_page_reports_no_cursor(
    listing: tuple[TaskListingService, IngestionControlPlaneDatabase],
) -> None:
    """A page that exhausts the query must not advertise more results."""
    service, control = listing
    await _seed_task(control, "task-1")

    page = await service.list_tasks(tenants=None, status=None, cursor=None, limit=50)

    assert [item["task_id"] for item in page["items"]] == ["task-1"]  # type: ignore[index]
    assert page["next_cursor"] is None


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_tenant_scope_excludes_other_tenants(
    listing: tuple[TaskListingService, IngestionControlPlaneDatabase],
) -> None:
    service, control = listing
    await _seed_task(control, "acme-1", tenant="ACME")
    await _seed_task(control, "other-1", tenant="OTHER")
    await _seed_task(control, "acme-2", tenant="ACME")

    scoped = await service.list_tasks(
        tenants=frozenset({"ACME"}),
        status=None,
        cursor=None,
        limit=50,
    )

    assert [item["task_id"] for item in scoped["items"]] == ["acme-2", "acme-1"]  # type: ignore[index]


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_empty_tenant_scope_returns_nothing(
    listing: tuple[TaskListingService, IngestionControlPlaneDatabase],
) -> None:
    """A principal with no tenants must not fall through to an unfiltered read."""
    service, control = listing
    await _seed_task(control, "task-1", tenant="ACME")

    page = await service.list_tasks(tenants=frozenset(), status=None, cursor=None, limit=50)

    assert page == {"items": [], "next_cursor": None}


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_status_filter_uses_the_public_status_name(
    listing: tuple[TaskListingService, IngestionControlPlaneDatabase],
) -> None:
    """SUCCESS is the public name for the durable COMPLETED state."""
    service, control = listing
    await _seed_task(control, "done-1")
    await _seed_task(control, "queued-1")
    await control.tasks.transition("done-1", IngestionTaskState.RUNNING)
    await control.tasks.transition("done-1", IngestionTaskState.COMPLETED)

    completed = await service.list_tasks(tenants=None, status="SUCCESS", cursor=None, limit=50)
    pending = await service.list_tasks(tenants=None, status="PENDING", cursor=None, limit=50)

    assert [item["task_id"] for item in completed["items"]] == ["done-1"]  # type: ignore[index]
    assert [item["status"] for item in completed["items"]] == ["SUCCESS"]  # type: ignore[index]
    assert [item["task_id"] for item in pending["items"]] == ["queued-1"]  # type: ignore[index]


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_progress_counts_come_from_the_recorded_outcomes(
    listing: tuple[TaskListingService, IngestionControlPlaneDatabase],
) -> None:
    """Listed rows carry the same progress the detail route reports."""
    service, control = listing
    await _seed_task(control, "task-1")
    await _seed_task(control, "task-2")
    for document_id, status in (("doc-1", "published"), ("doc-2", "failed")):
        await control.tasks.record_document_result(
            TaskDocumentResult(
                task_id="task-1",
                document_id=DocumentId(document_id),
                status=status,
                result={},
            )
        )

    page = await service.list_tasks(tenants=None, status=None, cursor=None, limit=50)
    progress = {str(item["task_id"]): item["progress"] for item in page["items"]}  # type: ignore[index]

    assert progress["task-1"]["processed"] == 2  # type: ignore[index]
    assert progress["task-1"]["succeeded"] == 1  # type: ignore[index]
    assert progress["task-1"]["failed"] == 1  # type: ignore[index]
    assert progress["task-2"]["processed"] == 0  # type: ignore[index]


@pytest.mark.asyncio
@pytest.mark.whitebox
@pytest.mark.parametrize("cursor", ["not-base64", "e30", "eyJ0YXNrX2lkIjoidGFzay0xIn0"])
async def test_malformed_cursors_are_rejected(
    listing: tuple[TaskListingService, IngestionControlPlaneDatabase],
    cursor: str,
) -> None:
    """Garbage, an empty object, and a position missing its timestamp all fail."""
    service, _ = listing

    with pytest.raises(IngestionCursorError):
        await service.list_tasks(tenants=None, status=None, cursor=cursor, limit=50)
