"""Summary accept and reconcile read only what their decision depends on."""

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from harborrag_adapters.repositories.database.ingestion_control import summary_authority
from harborrag_adapters.repositories.database.ingestion_control.summary_schema import (
    SUMMARY_BINDINGS,
)
from harborrag_core.base import utc_now
from harborrag_core.contracts import HarborConflictError
from harborrag_core.topology.permissions import ResolvedPermissionSnapshot

from .ingestion_control_fixtures import (
    advance_to_verified,
    candidate,
    make_control_plane,
    source_identity,
)
from .test_summary_projection import binding, node, prepare


def spy_snapshots(monkeypatch) -> list[tuple[str, str | None]]:
    calls: list[tuple[str, str | None]] = []
    original = summary_authority.SummaryAuthority._snapshot

    async def spy(self, session, tenant_id, source_scope_id, *, document_id=None):
        calls.append((source_scope_id, document_id))
        return await original(self, session, tenant_id, source_scope_id, document_id=document_id)

    monkeypatch.setattr(summary_authority.SummaryAuthority, "_snapshot", spy)
    return calls


@pytest.mark.asyncio
async def test_leaf_accept_reads_only_its_own_document(tmp_path, monkeypatch):
    async with make_control_plane(tmp_path) as control:
        version = await prepare(control)
        lease = await control.summaries.claim("DEFAULT")
        snapshot = await control.summaries.snapshot(lease)
        target = node(version)
        # Another document joins the scope without any permission snapshot. A
        # whole-scope re-read would refuse this unrelated card; a leaf card is
        # checked against its own document alone.
        other = candidate("other", source=source_identity("page-2"))
        await advance_to_verified(control, other)
        await control.publisher.publish(
            document_id=str(other.document_id),
            candidate_document_version_id=str(other.document_version_id),
        )
        calls = spy_snapshots(monkeypatch)
        await control.summaries.accept(lease, snapshot, binding(lease, snapshot, target), target)
        assert calls == [("scope-engineering", str(version.document_id))]


@pytest.mark.asyncio
async def test_leaf_accept_still_rejects_its_own_withdrawn_permission(tmp_path):
    async with make_control_plane(tmp_path) as control:
        version = await prepare(control)
        lease = await control.summaries.claim("DEFAULT")
        snapshot = await control.summaries.snapshot(lease)
        now = utc_now()
        await control.topology.set_permissions(
            ResolvedPermissionSnapshot(
                tenant_id="DEFAULT",
                resource_kind="document",
                resource_id=str(version.document_id),
                revision="acl-2",
                resolved_at=now,
                expires_at=now + timedelta(hours=1),
                known=True,
                processing_allowed=False,
                public=True,
            )
        )
        target = node(version)
        with pytest.raises(HarborConflictError, match="SUMMARY_PROCESSING_DISALLOWED"):
            await control.summaries.accept(
                lease, snapshot, binding(lease, snapshot, target), target
            )


@pytest.mark.asyncio
async def test_reconcile_skips_the_tenant_snapshot_without_a_dirty_idle_rollup(
    tmp_path, monkeypatch
):
    async with make_control_plane(tmp_path) as control:
        version = await prepare(control)
        lease = await control.summaries.claim("DEFAULT")
        snapshot = await control.summaries.snapshot(lease)
        target = node(version)
        await control.summaries.accept(lease, snapshot, binding(lease, snapshot, target), target)
        assert await control.summaries.finish(lease)

        calls = spy_snapshots(monkeypatch)
        # Current bindings: nothing to repair, and no '@tenant' policy, so the
        # tenant-wide snapshot is never read.
        assert await control.summaries.reconcile("DEFAULT") == 0
        assert calls == [("scope-engineering", None)]

        # A binding whose stored digest no longer matches is still repaired.
        async with control._client.sessions.begin() as session:
            stored = (
                await session.execute(
                    select(SUMMARY_BINDINGS.c.binding).where(
                        SUMMARY_BINDINGS.c.node_key == target.node_key
                    )
                )
            ).scalar_one()
            stored = {**stored, "manifest": {**stored["manifest"], "membership_digest": "old"}}
            await session.execute(
                update(SUMMARY_BINDINGS)
                .where(SUMMARY_BINDINGS.c.node_key == target.node_key)
                .values(binding=stored)
            )
        assert await control.summaries.reconcile("DEFAULT") == 1
