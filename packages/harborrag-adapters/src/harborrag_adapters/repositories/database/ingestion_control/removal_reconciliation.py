from __future__ import annotations

from collections.abc import Mapping, Set
from typing import cast

from sqlalchemy import case, func, select, update

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_core.base import utc_now
from harborrag_core.contracts import HarborConflictError, HarborNotFoundError
from harborrag_core.ingestion import SourceScanState

from .schema import SOURCE_ITEMS, SOURCE_SCANS


class SourceRemovalReconciler:
    """Apply consecutive misses only for the latest completed source scan."""

    def __init__(self, client: SQLAlchemyDBClient) -> None:
        self._client = client

    async def reconcile(
        self,
        scan_id: str,
        *,
        missing_threshold: int,
        immediate_binding_kinds: Set[str] = frozenset(),
    ) -> tuple[str, ...]:
        if missing_threshold < 1:
            raise ValueError("missing_threshold must be positive")
        async with self._client.sessions.begin() as session:
            scan_result = await session.execute(
                select(SOURCE_SCANS).where(SOURCE_SCANS.c.scan_id == scan_id).with_for_update()
            )
            scan = scan_result.mappings().one_or_none()
            if scan is None:
                raise HarborNotFoundError(f"source scan does not exist: {scan_id}")
            if scan["status"] != SourceScanState.COMPLETED.value:
                raise HarborConflictError("only a completed scan may reconcile removals")
            latest_result = await session.execute(
                select(func.max(SOURCE_SCANS.c.scan_sequence)).where(
                    SOURCE_SCANS.c.source_scope_id == scan["source_scope_id"],
                    SOURCE_SCANS.c.status == SourceScanState.COMPLETED.value,
                )
            )
            if latest_result.scalar_one() != scan["scan_sequence"]:
                return ()
            stale_result = await session.execute(
                select(SOURCE_ITEMS)
                .where(
                    SOURCE_ITEMS.c.source_scope_id == scan["source_scope_id"],
                    SOURCE_ITEMS.c.is_active.is_(True),
                    SOURCE_ITEMS.c.last_seen_scan_sequence < scan["scan_sequence"],
                )
                .with_for_update()
            )
            scan_sequence = cast(int, scan["scan_sequence"])
            stale = tuple(
                cast(Mapping[str, object], item)
                for item in stale_result.mappings().all()
                if item["last_reconciled_scan_sequence"] is None
                or cast(int, item["last_reconciled_scan_sequence"]) < scan_sequence
            )
            if not stale:
                return ()

            (
                misses_by_item,
                active_by_item,
                removed_keys,
                removed_documents,
            ) = _plan_reconciliation(
                stale,
                missing_threshold=missing_threshold,
                immediate_binding_kinds=immediate_binding_kinds,
            )

            documents_with_other_bindings: set[str] = set()
            if removed_documents:
                binding_result = await session.execute(
                    select(
                        SOURCE_ITEMS.c.source_scope_id,
                        SOURCE_ITEMS.c.source_item_id,
                        SOURCE_ITEMS.c.document_id,
                        SOURCE_ITEMS.c.is_active,
                    )
                    .where(SOURCE_ITEMS.c.document_id.in_(removed_documents))
                    .with_for_update()
                )
                for binding in binding_result.mappings().all():
                    key = (str(binding["source_scope_id"]), str(binding["source_item_id"]))
                    if bool(binding["is_active"]) and key not in removed_keys:
                        documents_with_other_bindings.add(str(binding["document_id"]))

            item_ids = tuple(misses_by_item)
            await session.execute(
                update(SOURCE_ITEMS)
                .where(
                    SOURCE_ITEMS.c.source_scope_id == scan["source_scope_id"],
                    SOURCE_ITEMS.c.source_item_id.in_(item_ids),
                )
                .values(
                    consecutive_misses=case(
                        misses_by_item,
                        value=SOURCE_ITEMS.c.source_item_id,
                        else_=SOURCE_ITEMS.c.consecutive_misses,
                    ),
                    last_reconciled_scan_sequence=scan_sequence,
                    is_active=case(
                        active_by_item,
                        value=SOURCE_ITEMS.c.source_item_id,
                        else_=SOURCE_ITEMS.c.is_active,
                    ),
                    updated_at=utc_now(),
                )
            )
            return tuple(sorted(removed_documents - documents_with_other_bindings))


def _plan_reconciliation(
    stale: tuple[Mapping[str, object], ...],
    *,
    missing_threshold: int,
    immediate_binding_kinds: Set[str],
) -> tuple[
    dict[str, int],
    dict[str, bool],
    set[tuple[str, str]],
    set[str],
]:
    misses_by_item: dict[str, int] = {}
    active_by_item: dict[str, bool] = {}
    removed_keys: set[tuple[str, str]] = set()
    removed_documents: set[str] = set()
    for item in stale:
        item_id = cast(str, item["source_item_id"])
        misses = cast(int, item["consecutive_misses"]) + 1
        threshold = 1 if str(item["binding_kind"]) in immediate_binding_kinds else missing_threshold
        remains_active = misses < threshold
        misses_by_item[item_id] = misses
        active_by_item[item_id] = remains_active
        if not remains_active:
            removed_keys.add((cast(str, item["source_scope_id"]), item_id))
            removed_documents.add(cast(str, item["document_id"]))
    return misses_by_item, active_by_item, removed_keys, removed_documents
