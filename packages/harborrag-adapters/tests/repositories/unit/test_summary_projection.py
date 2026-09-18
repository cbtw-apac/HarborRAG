"""Real SQL publication, lease, cache, and authorization acceptance scenarios."""

from datetime import timedelta

import pytest
from sqlalchemy import update

from harborrag_adapters.repositories.database.ingestion_control.summary_schema import SUMMARY_SCOPES
from harborrag_core.base import utc_now
from harborrag_core.contracts import HarborConflictError
from harborrag_core.ingestion import GraphNodeRecord
from harborrag_core.summaries import SummaryBinding, SummaryCard, SummaryManifest, SummaryPolicy
from harborrag_core.topology.config import TenantIndexingConfig
from harborrag_core.topology.permissions import ResolvedPermissionSnapshot

from .ingestion_control_fixtures import (
    advance_to_verified,
    candidate,
    make_control_plane,
    source_identity,
)
from .topology_fixtures import ACCESS, permit


async def prepare(control, content="one"):
    version = candidate(content)
    await permit(control, str(version.document_id))
    await advance_to_verified(control, version)
    await control.publisher.publish(
        document_id=str(version.document_id),
        candidate_document_version_id=str(version.document_version_id),
    )
    await control.summaries.configure(
        "DEFAULT",
        "scope-engineering",
        SummaryPolicy(model_fingerprint="model-v1", debounce_seconds=0),
    )
    return version


def node(version):
    return GraphNodeRecord(
        node_key="version:" + str(version.document_version_id),
        node_kind="DocumentVersion",
        logical_id=str(version.document_version_id),
        ownership_scope="DOCUMENT_VERSION",
        owner_id="DEFAULT",
        source_scope_id="scope-engineering",
        document_id=version.document_id,
        document_version_id=version.document_version_id,
        title="Guide",
    )


def binding(lease, snapshot, target):
    card = SummaryCard(description="Deployment guide.", topics=("deployment",))
    return SummaryBinding(
        manifest=SummaryManifest(
            node_key=target.node_key,
            kind="DocumentVersion",
            source_scope_id=lease.source_scope_id,
            input_document_versions=snapshot.document_versions,
            permission_dependencies=snapshot.permission_dependencies,
            policy_fingerprint=lease.policy.fingerprint,
            membership_digest=snapshot.membership_digest,
            input_digest="input",
            input_chunk_ids=("chunk",),
        ),
        card=card,
        generation_key="generation",
        artifact_hash=card.artifact_hash,
        revision=lease.revision,
        updated_at=utc_now(),
    )


@pytest.mark.asyncio
async def test_publish_intent_replays_without_invalidating_and_new_publication_fences_old_work(
    tmp_path,
):
    async with make_control_plane(tmp_path) as control:
        version = await prepare(control)
        lease = await control.summaries.claim("DEFAULT")
        snapshot = await control.summaries.snapshot(lease)
        target = node(version)
        value = binding(lease, snapshot, target)
        await control.publisher.publish(
            document_id=str(version.document_id),
            candidate_document_version_id=str(version.document_version_id),
        )
        await control.summaries.accept(lease, snapshot, value, target)
        assert (await control.summaries.views("DEFAULT", (target.node_key,), access=ACCESS))[
            target.node_key
        ].status == "current"
        newer = candidate("two")
        await advance_to_verified(control, newer)
        await control.publisher.publish(
            document_id=str(newer.document_id),
            candidate_document_version_id=str(newer.document_version_id),
        )
        with pytest.raises(HarborConflictError):
            await control.summaries.accept(lease, snapshot, value, target)
        view = (await control.summaries.views("DEFAULT", (target.node_key,), access=ACCESS))[
            target.node_key
        ]
        assert view.status == "stale" and view.card is None


@pytest.mark.asyncio
async def test_revocation_hides_every_card_field_and_rebinding_requires_current_acl(tmp_path):
    async with make_control_plane(tmp_path) as control:
        version = await prepare(control)
        lease = await control.summaries.claim("DEFAULT")
        snapshot = await control.summaries.snapshot(lease)
        target = node(version)
        await control.summaries.accept(lease, snapshot, binding(lease, snapshot, target), target)
        now = utc_now()
        await control.topology.set_permissions(
            ResolvedPermissionSnapshot(
                tenant_id="DEFAULT",
                resource_kind="document",
                resource_id=str(version.document_id),
                revision="revoked",
                resolved_at=now,
                expires_at=now + timedelta(hours=1),
                known=True,
                processing_allowed=True,
                public=False,
            )
        )
        view = (await control.summaries.views("DEFAULT", (target.node_key,), access=ACCESS))[
            target.node_key
        ]
        assert view.card is None and view.included_chunks is None and view.artifact_hash is None
        with pytest.raises(HarborConflictError):
            await control.summaries.snapshot(lease)


@pytest.mark.asyncio
async def test_cache_selects_one_winner_and_never_crosses_tenants(tmp_path):
    async with make_control_plane(tmp_path) as control:
        first = SummaryCard(description="First result.")
        second = SummaryCard(description="Nondeterministic second result.")
        assert await control.summaries.put_card("one", "key", first) == first
        assert await control.summaries.put_card("one", "key", second) == first
        assert await control.summaries.get_card("two", "key") is None


@pytest.mark.asyncio
async def test_expired_lease_can_be_reclaimed_but_old_worker_cannot_publish(tmp_path):
    async with make_control_plane(tmp_path) as control:
        version = await prepare(control)
        first = await control.summaries.claim("DEFAULT")
        snapshot = await control.summaries.snapshot(first)
        async with control._client.sessions.begin() as session:
            await session.execute(
                update(SUMMARY_SCOPES).values(lease_until=utc_now() - timedelta(seconds=1))
            )
        second = await control.summaries.claim("DEFAULT")
        assert second.fence > first.fence
        with pytest.raises(HarborConflictError):
            await control.summaries.accept(
                first, snapshot, binding(first, snapshot, node(version)), node(version)
            )


@pytest.mark.asyncio
async def test_retirement_invalidates_without_waiting_for_summary_worker(tmp_path):
    async with make_control_plane(tmp_path) as control:
        version = await prepare(control)
        lease = await control.summaries.claim("DEFAULT")
        snapshot = await control.summaries.snapshot(lease)
        target = node(version)
        await control.summaries.accept(lease, snapshot, binding(lease, snapshot, target), target)
        await control.publisher.retire_removed(document_id=str(version.document_id))
        view = (await control.summaries.views("DEFAULT", (target.node_key,), access=ACCESS))[
            target.node_key
        ]
        assert view.status == "stale" and view.card is None


@pytest.mark.asyncio
async def test_unrelated_publication_preserves_immutable_document_work(tmp_path):
    async with make_control_plane(tmp_path) as control:
        version = await prepare(control)
        lease = await control.summaries.claim("DEFAULT")
        snapshot = await control.summaries.snapshot(lease)
        target = node(version)
        other = candidate("other", source=source_identity("page-2"))
        await permit(control, str(other.document_id))
        await advance_to_verified(control, other)
        await control.publisher.publish(
            document_id=str(other.document_id),
            candidate_document_version_id=str(other.document_version_id),
        )
        await control.summaries.renew(lease)
        await control.summaries.accept(lease, snapshot, binding(lease, snapshot, target), target)
        view = (await control.summaries.views("DEFAULT", (target.node_key,), access=ACCESS))[
            target.node_key
        ]
        assert view.status == "current"
        assert await control.summaries.document_bindings(
            "DEFAULT", str(version.document_id), str(version.document_version_id)
        )
        assert not await control.summaries.finish(lease)
        assert await control.summaries.claim("DEFAULT") is None  # coalesced retry delay


@pytest.mark.asyncio
async def test_policy_aba_never_revives_old_worker_and_duplicate_claim_is_idle(tmp_path):
    async with make_control_plane(tmp_path) as control:
        version = await prepare(control)
        first = await control.summaries.claim("DEFAULT")
        assert await control.summaries.claim("DEFAULT") is None
        snapshot = await control.summaries.snapshot(first)
        await control.summaries.configure(
            "DEFAULT",
            "scope-engineering",
            SummaryPolicy(model_fingerprint="model-v2", debounce_seconds=0),
        )
        await control.summaries.configure("DEFAULT", "scope-engineering", first.policy)
        second = await control.summaries.claim("DEFAULT")
        assert second.fence > first.fence
        with pytest.raises(HarborConflictError):
            await control.summaries.accept(
                first, snapshot, binding(first, snapshot, node(version)), node(version)
            )


@pytest.mark.asyncio
async def test_status_runnable_reconcile_and_fenced_finish_paths(tmp_path):
    async with make_control_plane(tmp_path) as control:
        await prepare(control)
        policy = SummaryPolicy(model_fingerprint="model-v1", debounce_seconds=0)
        await control.summaries.configure("DEFAULT", "scope-engineering", policy)
        runnable = await control.summaries.runnable_scopes("DEFAULT")
        assert len(runnable) == 1
        assert runnable[0][0] == "scope-engineering" and runnable[0][2] == 0
        assert "scope-engineering" in await control.summaries.source_scope_ids("DEFAULT")
        assert (await control.summaries.status("DEFAULT"))[0]["execution"] == "queued"

        lease = await control.summaries.claim("DEFAULT", source_scope_id="scope-engineering")
        assert lease is not None
        assert not await control.summaries.finish(lease.model_copy(update={"fence": 99}))
        assert await control.summaries.finish(lease)
        assert await control.summaries.reconcile("DEFAULT") == 1


@pytest.mark.asyncio
async def test_permission_block_waits_for_refresh_even_when_revision_is_unchanged(tmp_path):
    async with make_control_plane(tmp_path) as control:
        version = await prepare(control)
        lease = await control.summaries.claim("DEFAULT")
        assert lease is not None
        await control.summaries.finish(
            lease, error_code="SUMMARY_PERMISSION_SNAPSHOT_MISSING", blocked=True
        )
        assert await control.summaries.runnable_scopes("DEFAULT") == ()
        assert await control.summaries.claim("DEFAULT") is None
        now = utc_now()
        await control.topology.set_permissions(
            ResolvedPermissionSnapshot(
                tenant_id="DEFAULT",
                resource_kind="document",
                resource_id=str(version.document_id),
                revision="acl-1",
                resolved_at=now,
                expires_at=now + timedelta(hours=2),
                known=True,
                processing_allowed=True,
                public=True,
            )
        )
        assert (await control.summaries.runnable_scopes("DEFAULT"))[0][0] == ("scope-engineering")


@pytest.mark.asyncio
async def test_prohibited_tenant_and_removed_policy_do_not_dispatch(tmp_path):
    async with make_control_plane(tmp_path) as control:
        await prepare(control)
        await control.topology.configure_indexing(
            TenantIndexingConfig(tenant_id="DEFAULT", enabled=True, prohibited=True)
        )
        assert await control.summaries.claim("DEFAULT") is None

        await control.topology.configure_indexing(
            TenantIndexingConfig(tenant_id="DEFAULT", enabled=True)
        )
        lease = await control.summaries.claim("DEFAULT")
        assert lease is not None
        await control.summaries.configure("DEFAULT", "scope-engineering", None)
        assert not await control.summaries.finish(lease)
        assert not await control.summaries.runnable_scopes("DEFAULT")
