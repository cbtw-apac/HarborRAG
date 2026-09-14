"""Retry candidate selection for public ingestion tasks."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from harborrag_core.invariants import require

from .ports import PublicTaskStore

_RETRY_PAGE_SIZE = 200


async def retryable_document_ids(
    store: PublicTaskStore,
    *,
    task_id: str,
    requested: Sequence[str],
) -> tuple[str, ...]:
    requested_set = set(requested)
    selected: list[str] = []
    after_updated_at: datetime | None = None
    after_document_id: str | None = None
    while True:
        page = await store.document_results_page(
            task_id,
            statuses=("failed",),
            after_updated_at=after_updated_at,
            after_document_id=after_document_id,
            limit=_RETRY_PAGE_SIZE,
        )
        for result in page.items:
            document_id = str(result.document_id)
            if requested_set and document_id not in requested_set:
                continue
            if result.result.get("retryable") is True:
                selected.append(document_id)
        if not page.has_more or not page.items:
            break
        last = page.items[-1]
        after_updated_at = require(
            last.updated_at,
            "paged document result is missing updated_at",
        )
        after_document_id = str(last.document_id)
    return tuple(dict.fromkeys(selected))
