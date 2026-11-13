"""`FailuresService` behind `GET /v1/admin/failures`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from harborrag_app.workflow_control.ingestion.failures import FailuresService
from harborrag_core.ingestion import DocumentFailureRecord
from harborrag_core.ingestion.states import FailureCategory
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId


class _FakeStore:
    def __init__(self, failures: tuple[DocumentFailureRecord, ...]) -> None:
        self._failures = failures
        self.calls: list[dict[str, object]] = []

    async def list_failures(
        self,
        *,
        tenant_id: str,
        connection_id: str,
        limit: int = 100,
    ) -> tuple[DocumentFailureRecord, ...]:
        self.calls.append({"tenant_id": tenant_id, "connection_id": connection_id, "limit": limit})
        return self._failures


def _record(attempts: int) -> DocumentFailureRecord:
    now = datetime.now(UTC)
    return DocumentFailureRecord(
        document_id=DocumentId("doc-1"),
        document_version_id=DocumentVersionId("doc-1@v1"),
        failed_stage="ParseAndNormalize",
        category=FailureCategory.PARSER_FALLBACKABLE,
        retryable=True,
        safe_error_code="parser_rejected_document",
        attempt_count=attempts,
        first_failure_at=now,
        last_failure_at=now,
    )


@pytest.mark.asyncio
async def test_list_failures_passes_tenant_connection_and_limit_through() -> None:
    expected = (_record(2),)
    store = _FakeStore(expected)

    async def task_registry() -> _FakeStore:
        return store

    service = FailuresService(task_registry=task_registry)  # type: ignore[arg-type]

    result = await service.list_failures(
        tenant_id="cpm", connection_id="cpm/cpm-tickets", limit=10
    )

    assert result == expected
    assert store.calls == [
        {"tenant_id": "cpm", "connection_id": "cpm/cpm-tickets", "limit": 10}
    ]
