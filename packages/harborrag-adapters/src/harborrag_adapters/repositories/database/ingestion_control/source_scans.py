from __future__ import annotations

from collections.abc import Mapping, Sequence, Set
from typing import cast
from uuid import uuid4

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_core.base import utc_now
from harborrag_core.contracts import HarborConflictError, HarborNotFoundError
from harborrag_core.ingestion import (
    DiscoveredSourceItem,
    SourceItemRegistration,
    SourceScanState,
    StoredSourceItem,
)

from .removal_reconciliation import SourceRemovalReconciler
from .schema import SOURCE_ITEMS, SOURCE_SCANS, SOURCE_SCOPES
from .source_item_mapping import (
    registration_from_existing,
    stored_source_item_from_row,
)


class SourceScanRepository:
    """Persist authoritative discovery scans and consecutive removal misses."""

    def __init__(self, client: SQLAlchemyDBClient) -> None:
        self._client = client
        self._removals = SourceRemovalReconciler(client)

    async def register_scope(
        self,
        *,
        tenant_id: str = "DEFAULT",
        source_scope_id: str,
        connector_type: str,
        connection_id: str,
        configuration_fingerprint: str,
    ) -> None:
        now = utc_now()
        async with self._client.sessions.begin() as session:
            row = await session.execute(
                select(SOURCE_SCOPES).where(SOURCE_SCOPES.c.source_scope_id == source_scope_id)
            )
            existing = row.mappings().one_or_none()
            if existing is None:
                await session.execute(
                    insert(SOURCE_SCOPES).values(
                        source_scope_id=source_scope_id,
                        tenant_id=tenant_id,
                        connector_type=connector_type,
                        connection_id=connection_id,
                        configuration_fingerprint=configuration_fingerprint,
                        created_at=now,
                        updated_at=now,
                    )
                )
                return
            if existing["tenant_id"] != tenant_id:
                raise HarborConflictError("source scope belongs to another tenant")
            await session.execute(
                update(SOURCE_SCOPES)
                .where(SOURCE_SCOPES.c.source_scope_id == source_scope_id)
                .values(
                    connector_type=connector_type,
                    connection_id=connection_id,
                    configuration_fingerprint=configuration_fingerprint,
                    updated_at=now,
                )
            )

    async def start(self, source_scope_id: str, *, scan_id: str | None = None) -> str:
        selected_scan_id = scan_id or f"scan:{uuid4().hex}"
        async with self._client.sessions.begin() as session:
            scope = await session.execute(
                select(SOURCE_SCOPES.c.source_scope_id)
                .where(SOURCE_SCOPES.c.source_scope_id == source_scope_id)
                .with_for_update()
            )
            if scope.scalar_one_or_none() is None:
                raise HarborNotFoundError(f"source scope does not exist: {source_scope_id}")
            selected = await session.execute(
                select(SOURCE_SCANS).where(SOURCE_SCANS.c.scan_id == selected_scan_id)
            )
            existing = selected.mappings().one_or_none()
            if existing is not None:
                if existing["source_scope_id"] != source_scope_id:
                    raise HarborConflictError(
                        "source scan identity belongs to a different source scope"
                    )
                if existing["status"] == SourceScanState.STARTED.value:
                    return selected_scan_id
                raise HarborConflictError("a finished source scan cannot be restarted")
            running = await session.execute(
                select(SOURCE_SCANS.c.scan_id).where(
                    SOURCE_SCANS.c.source_scope_id == source_scope_id,
                    SOURCE_SCANS.c.status == SourceScanState.STARTED.value,
                )
            )
            if running.scalar_one_or_none() is not None:
                raise HarborConflictError(
                    f"an authoritative scan is already running for scope {source_scope_id}"
                )
            sequence_result = await session.execute(
                select(func.max(SOURCE_SCANS.c.scan_sequence)).where(
                    SOURCE_SCANS.c.source_scope_id == source_scope_id
                )
            )
            sequence = int(sequence_result.scalar_one_or_none() or 0) + 1
            await session.execute(
                insert(SOURCE_SCANS).values(
                    scan_id=selected_scan_id,
                    source_scope_id=source_scope_id,
                    scan_sequence=sequence,
                    status=SourceScanState.STARTED.value,
                    started_at=utc_now(),
                    seen_count=0,
                )
            )
        return selected_scan_id

    async def record_seen(
        self,
        *,
        scan_id: str,
        item: DiscoveredSourceItem,
    ) -> SourceItemRegistration:
        return (await self.record_seen_many(scan_id=scan_id, items=(item,)))[0]

    async def record_seen_many(
        self,
        *,
        scan_id: str,
        items: Sequence[DiscoveredSourceItem],
    ) -> tuple[SourceItemRegistration, ...]:
        """Record a bounded discovery batch in one scan transaction."""

        if not items:
            return ()
        registrations: list[SourceItemRegistration] = []
        async with self._client.sessions.begin() as session:
            scan = await self._started_scan(session, scan_id)
            newly_seen = 0
            for item in items:
                source = item.source_identity
                if scan["source_scope_id"] != source.source_scope_id:
                    raise HarborConflictError("source item belongs to a different source scope")
                key_condition = (
                    SOURCE_ITEMS.c.source_scope_id == source.source_scope_id,
                    SOURCE_ITEMS.c.source_item_id == source.source_item_id,
                )
                result = await session.execute(select(SOURCE_ITEMS).where(*key_condition))
                existing = cast(
                    Mapping[str, object] | None,
                    result.mappings().one_or_none(),
                )
                already_seen = (
                    existing is not None
                    and existing["last_seen_scan_sequence"] == scan["scan_sequence"]
                )
                values = {
                    "document_id": str(item.document_id),
                    "source_version": item.source_version,
                    "binding_kind": source.binding.kind.value,
                    "parent_source_item_id": source.binding.parent_source_item_id,
                    "admission_change_key": item.admission_change_key,
                    "last_seen_scan_sequence": scan["scan_sequence"],
                    "consecutive_misses": 0,
                    "is_active": True,
                    "descriptor": dict(item.descriptor),
                    "updated_at": utc_now(),
                }
                if existing is None:
                    await session.execute(
                        insert(SOURCE_ITEMS).values(
                            source_scope_id=source.source_scope_id,
                            source_item_id=source.source_item_id,
                            **values,
                        )
                    )
                else:
                    if existing["document_id"] != str(item.document_id):
                        raise HarborConflictError(
                            "source identity resolved to a different document identity"
                        )
                    await session.execute(
                        update(SOURCE_ITEMS).where(*key_condition).values(**values)
                    )
                if not already_seen:
                    newly_seen += 1
                registrations.append(registration_from_existing(existing, item))
            if newly_seen:
                await session.execute(
                    update(SOURCE_SCANS)
                    .where(SOURCE_SCANS.c.scan_id == scan_id)
                    .values(seen_count=SOURCE_SCANS.c.seen_count + newly_seen)
                )
        return tuple(registrations)

    async def complete(
        self,
        scan_id: str,
        *,
        discovery_cursor: Mapping[str, object] | None = None,
    ) -> None:
        await self._finish(
            scan_id,
            state=SourceScanState.COMPLETED,
            discovery_cursor=discovery_cursor,
        )

    async def fail(self, scan_id: str, *, safe_reason: str) -> None:
        await self._finish(
            scan_id,
            state=SourceScanState.FAILED,
            failure_reason=safe_reason,
        )

    async def fail_if_started(self, scan_id: str, *, safe_reason: str) -> bool:
        """Fail an open deterministic scan while keeping terminal replays safe."""

        if not safe_reason.strip():
            raise ValueError("source scan failure reason must be non-empty")
        async with self._client.sessions.begin() as session:
            result = await session.execute(
                select(SOURCE_SCANS).where(SOURCE_SCANS.c.scan_id == scan_id).with_for_update()
            )
            scan = result.mappings().one_or_none()
            if scan is None or scan["status"] != SourceScanState.STARTED.value:
                return False
            await session.execute(
                update(SOURCE_SCANS)
                .where(SOURCE_SCANS.c.scan_id == scan_id)
                .values(
                    status=SourceScanState.FAILED.value,
                    completed_at=utc_now(),
                    failure_reason=safe_reason,
                )
            )
            return True

    async def cancel(self, scan_id: str) -> None:
        await self._finish(scan_id, state=SourceScanState.CANCELLED)

    async def reconcile_removals(
        self,
        scan_id: str,
        *,
        missing_threshold: int = 2,
        immediate_binding_kinds: Set[str] = frozenset(),
    ) -> tuple[str, ...]:
        return await self._removals.reconcile(
            scan_id,
            missing_threshold=missing_threshold,
            immediate_binding_kinds=immediate_binding_kinds,
        )

    async def source_item(
        self,
        *,
        source_scope_id: str,
        source_item_id: str,
    ) -> StoredSourceItem | None:
        async with self._client.sessions() as session:
            result = await session.execute(
                select(SOURCE_ITEMS, SOURCE_SCOPES)
                .join(
                    SOURCE_SCOPES,
                    SOURCE_SCOPES.c.source_scope_id == SOURCE_ITEMS.c.source_scope_id,
                )
                .where(
                    SOURCE_ITEMS.c.source_scope_id == source_scope_id,
                    SOURCE_ITEMS.c.source_item_id == source_item_id,
                )
            )
            row = result.mappings().one_or_none()
            if row is None:
                return None
            return stored_source_item_from_row(row)

    async def _finish(
        self,
        scan_id: str,
        *,
        state: SourceScanState,
        discovery_cursor: Mapping[str, object] | None = None,
        failure_reason: str | None = None,
    ) -> None:
        async with self._client.sessions.begin() as session:
            result = await session.execute(
                select(SOURCE_SCANS).where(SOURCE_SCANS.c.scan_id == scan_id).with_for_update()
            )
            scan = result.mappings().one_or_none()
            if scan is None:
                raise HarborNotFoundError(f"source scan does not exist: {scan_id}")
            if scan["status"] == state.value:
                return
            if scan["status"] != SourceScanState.STARTED.value:
                raise HarborConflictError(
                    "a finished source scan cannot transition to another terminal state"
                )
            await session.execute(
                update(SOURCE_SCANS)
                .where(SOURCE_SCANS.c.scan_id == scan_id)
                .values(
                    status=state.value,
                    completed_at=utc_now(),
                    discovery_cursor=(
                        dict(discovery_cursor) if discovery_cursor is not None else None
                    ),
                    failure_reason=failure_reason,
                )
            )

    @staticmethod
    async def _started_scan(
        session: AsyncSession,
        scan_id: str,
    ) -> Mapping[str, object]:
        result = await session.execute(
            select(SOURCE_SCANS).where(SOURCE_SCANS.c.scan_id == scan_id).with_for_update()
        )
        scan = result.mappings().one_or_none()
        if scan is None:
            raise HarborNotFoundError(f"source scan does not exist: {scan_id}")
        if scan["status"] != SourceScanState.STARTED.value:
            raise HarborConflictError("source scan is no longer accepting discovery records")
        return cast(Mapping[str, object], scan)
