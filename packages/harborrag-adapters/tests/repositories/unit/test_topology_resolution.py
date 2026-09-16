from pathlib import Path

import pytest
from sqlalchemy import update

from harborrag_adapters.repositories.database.ingestion_control.topology.schema import (
    TOPOLOGY_MENTIONS,
)
from harborrag_core.contracts import HarborConflictError
from harborrag_core.topology import ResolutionRequest

from .ingestion_control_fixtures import make_control_plane
from .topology_fixtures import ACCESS, policy, prepared_build, publish


def request() -> ResolutionRequest:
    return ResolutionRequest(
        tenant_id="DEFAULT",
        decision_id="merge-1",
        action="merge",
        entity_ids=("opaque-1", "opaque-2"),
        actor="operator",
        reason="Verified same service",
    )


@pytest.mark.asyncio
async def test_manual_merge_rebuild_and_reversal_are_audited_without_reextraction(
    tmp_path: Path,
) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.topology.configure_policy(policy())
        await publish(control)
        original_job = await control.topology.claim("DEFAULT")
        assert original_job is not None
        original = await prepared_build(control, original_job)
        await control.topology.stage(original_job, original)
        await control.topology.mark_verified(original_job, original.build_id)
        assert await control.topology.accept(original_job, original.build_id)

        decision = await control.topology.record_resolution(request())
        assert decision.revision == 1 and decision.resolution_revision == "manual:1"
        assert await control.topology.record_resolution(request()) == decision
        assert await control.topology.active_mentions("DEFAULT", access=ACCESS) == ()
        assert await control.topology.get_build("DEFAULT", original.build_id) == original
        assert not await control.topology.accept(original_job, original.build_id)
        assert await control.topology.reconcile("DEFAULT") == 1
        current = await control.topology.claim("DEFAULT")
        assert current is not None
        assert current.document_version_id == original_job.document_version_id
        assert current.policy.profile.fingerprint == original_job.policy.profile.fingerprint
        assert await control.topology.resolve_entities(
            current, ("opaque-1", "opaque-2", "unlinked")
        ) == {
            "opaque-1": "opaque-1",
            "opaque-2": "opaque-1",
            "unlinked": "unlinked",
        }
        await control.topology.fail(current, "test_retry")

        reverted = await control.topology.record_resolution(
            ResolutionRequest(
                tenant_id="DEFAULT",
                decision_id="revert-1",
                action="revert",
                reverts_decision_id="merge-1",
                actor="operator",
                reason="Distinct services established",
            )
        )
        assert reverted.revision == 2
        assert await control.topology.reconcile("DEFAULT") == 1
        after_revert = await control.topology.claim("DEFAULT")
        assert after_revert is not None
        assert await control.topology.resolve_entities(after_revert, ("opaque-1", "opaque-2")) == {
            "opaque-1": "opaque-1",
            "opaque-2": "opaque-2",
        }
        assert len(await control.topology.list_resolutions("DEFAULT")) == 2
        assert await control.topology.list_resolutions("other") == ()
        # New source policies inherit the tenant resolution snapshot; normal configure
        # cannot accidentally revert the accepted identity policy to conservative-v1.
        await control.topology.configure_policy(
            policy().model_copy(update={"source_scope_id": "new-scope"})
        )
        configured = await control.topology.get_policy("DEFAULT", "new-scope")
        assert configured is not None and configured.resolution_revision == "manual:2"


@pytest.mark.asyncio
async def test_resolution_rejects_unknown_cross_tenant_and_incompatible_entities(
    tmp_path: Path,
) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.topology.configure_policy(policy())
        await publish(control)
        job = await control.topology.claim("DEFAULT")
        assert job is not None
        build = await prepared_build(control, job)
        await control.topology.stage(job, build)
        with pytest.raises(HarborConflictError, match="this tenant"):
            await control.topology.record_resolution(
                request().model_copy(update={"tenant_id": "other"})
            )
        with pytest.raises(HarborConflictError, match="this tenant"):
            await control.topology.record_resolution(
                request().model_copy(update={"entity_ids": ("opaque-1", "missing")})
            )
        changed = build.mentions[1].model_copy(
            update={
                "observation": build.mentions[1].observation.model_copy(
                    update={"entity_type": "person"}
                ),
            }
        )
        async with control._client.sessions.begin() as session:
            await session.execute(
                update(TOPOLOGY_MENTIONS)
                .where(
                    TOPOLOGY_MENTIONS.c.mention_id == changed.mention_id,
                )
                .values(record=changed.model_dump(mode="json"))
            )
        with pytest.raises(HarborConflictError, match="matching entity types"):
            await control.topology.record_resolution(request())
        with pytest.raises(HarborConflictError, match="checksum"):
            await control.topology.get_build("DEFAULT", build.build_id)


@pytest.mark.asyncio
async def test_resolution_decision_id_and_reversal_validation(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.topology.configure_policy(policy())
        await publish(control)
        job = await control.topology.claim("DEFAULT")
        assert job is not None
        await control.topology.stage(job, await prepared_build(control, job))
        await control.topology.record_resolution(request())
        with pytest.raises(HarborConflictError, match="immutable"):
            await control.topology.record_resolution(
                request().model_copy(update={"reason": "Changed explanation"})
            )
        reversal = ResolutionRequest(
            tenant_id="DEFAULT",
            decision_id="reversal",
            action="revert",
            reverts_decision_id="missing",
            reason="test",
            actor="operator",
        )
        with pytest.raises(HarborConflictError, match="existing tenant merge"):
            await control.topology.record_resolution(reversal)
        reversal = reversal.model_copy(update={"reverts_decision_id": "merge-1"})
        await control.topology.record_resolution(reversal)
        with pytest.raises(HarborConflictError, match="already reverted"):
            await control.topology.record_resolution(
                reversal.model_copy(update={"decision_id": "reversal-2"})
            )
