from __future__ import annotations

import pytest
from harborrag_app.workflow_control.ingestion.models import IngestionCreateCommand

from harborrag_core.ingestion import IngestionTaskState, TaskDocumentResult


def _command(*, marker: str = "one") -> IngestionCreateCommand:
    return IngestionCreateCommand(
        tenant_id="ACME",
        connection_id="harborrag-workspace",
        force_reprocess=False,
        public_request={
            "connection_id": "harborrag-workspace",
            "tenant": "ACME",
            "marker": marker,
        },
    )


@pytest.mark.asyncio
async def test_status_and_cursor_pages_are_read_from_the_database(service_resources) -> None:
    service, control, temporal = service_resources
    accepted = await service.submit(_command(), idempotency_key=None)
    task_id = str(accepted["task_id"])
    await control.tasks.transition(task_id, IngestionTaskState.RUNNING)
    await control.tasks.update_summary(
        task_id,
        {"stage": "PROCESSING_DOCUMENTS", "discovered": 3, "admitted": 3},
    )
    for index in range(3):
        await control.tasks.record_document_result(
            TaskDocumentResult(
                task_id=task_id,
                document_id=f"document:{index}",
                status="published",
                result={
                    "source_item_id": f"adr/{index}.md",
                    "document_kind": "file",
                    "title": f"ADR-{index}",
                },
            )
        )

    task = await service.get_task(task_id)
    first = await service.list_documents(task_id=task_id, status="SUCCESS", cursor=None, limit=2)
    second = await service.list_documents(
        task_id=task_id,
        status="SUCCESS",
        cursor=str(first["next_cursor"]),
        limit=2,
    )

    assert task["progress"] == {
        "discovered": 3,
        "admitted": 3,
        "processed": 3,
        "succeeded": 3,
        "failed": 0,
        "skipped": 0,
        "removed": 0,
    }
    assert len(first["items"]) == 2
    assert len(second["items"]) == 1
    assert len(temporal.started) == 1
