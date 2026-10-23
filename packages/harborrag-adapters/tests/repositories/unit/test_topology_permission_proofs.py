import asyncio
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from harborrag_core.base import utc_now
from harborrag_core.security.context import AccessContext
from harborrag_core.topology import ResolutionRequest
from harborrag_core.topology.budget import BudgetRequest, IndexingBudgetLimits
from harborrag_core.topology.config import TenantIndexingConfig

from .ingestion_control_fixtures import (
    advance_to_verified,
    candidate,
    make_control_plane,
    source_identity,
)
from .test_topology_permissions_budget import acl
from .topology_fixtures import ACCESS, permit, policy, prepared_build, publish


@pytest.mark.asyncio
async def test_expired_snapshot_same_revision_refresh_requeues_blocked_generation(
    tmp_path: Path,
) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.topology.configure_policy(policy())
        await publish(control)
        version = candidate("one")
        now = utc_now()
        expired = acl(
            str(version.document_id),
            resolved_at=now - timedelta(hours=2),
            expires_at=now - timedelta(hours=1),
        )
        # A newly discovered document may have only an already-expired ACL snapshot.
        second = candidate("two", source=source_identity("page-2"))
        await control.topology.set_permissions(
            expired.model_copy(update={"resource_id": str(second.document_id)})
        )
        await advance_to_verified(control, second)
        await control.publisher.publish(
            document_id=str(second.document_id),
            candidate_document_version_id=str(second.document_version_id),
        )
        assert await control.topology.reconcile("DEFAULT") == 0
        await control.topology.set_permissions(acl(str(second.document_id)))
        assert await control.topology.reconcile("DEFAULT") == 1
        assert await control.topology.reconcile("DEFAULT") == 0


@pytest.mark.asyncio
async def test_manual_identity_bridge_requires_all_private_proofs_before_serving(
    tmp_path: Path,
) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.topology.configure_policy(policy())
        await publish(control)
        first = await control.topology.claim("DEFAULT")
        assert first is not None
        original = await prepared_build(control, first)
        await control.topology.stage(first, original)
        await control.topology.mark_verified(first, original.build_id)
        assert await control.topology.accept(first, original.build_id)

        private = candidate("private", source=source_identity("private-page"))
        await permit(control, str(private.document_id))
        await control.topology.set_permissions(
            acl(str(private.document_id), public=False, allowed_principal_ids=("privileged",))
        )
        await advance_to_verified(control, private)
        await control.publisher.publish(
            document_id=str(private.document_id),
            candidate_document_version_id=str(private.document_version_id),
        )
        second = await control.topology.claim("DEFAULT")
        assert second is not None
        shadow = await prepared_build(control, second)
        private_build_id = "private-build"
        mentions = tuple(
            item.model_copy(
                update={
                    "build_id": private_build_id,
                    "mention_id": f"private-{item.mention_id}",
                    "entity_id": f"private-{item.entity_id}",
                }
            )
            for item in shadow.mentions
        )
        assertions = tuple(
            item.model_copy(
                update={
                    "build_id": private_build_id,
                    "assertion_id": f"private-{item.assertion_id}",
                    "subject_entity_id": f"private-{item.subject_entity_id}",
                    "object_entity_id": f"private-{item.object_entity_id}",
                }
            )
            for item in shadow.assertions
        )
        shadow = shadow.model_copy(
            update={"build_id": private_build_id, "mentions": mentions, "assertions": assertions}
        )
        await control.topology.stage(second, shadow)
        await control.topology.mark_verified(second, shadow.build_id)
        assert await control.topology.accept(second, shadow.build_id)
        await control.topology.record_resolution(
            ResolutionRequest(
                tenant_id="DEFAULT",
                decision_id="private-proof",
                action="merge",
                entity_ids=("opaque-1", "private-opaque-1"),
                actor="operator",
                reason="verified source identities",
            )
        )
        assert await control.topology.reconcile("DEFAULT") == 2
        jobs = await control.topology.list_jobs("DEFAULT")
        selected = next(
            value
            for value in jobs
            if value.document_id == first.document_id
            and value.policy_revision > first.policy_revision
        )
        current = await control.topology.claim("DEFAULT", job_id=selected.job_id)
        assert current is not None
        remapped = await prepared_build(control, current)
        await control.topology.stage(current, remapped)
        await control.topology.mark_verified(current, remapped.build_id)
        assert await control.topology.accept(current, remapped.build_id)
        # The public document itself remains readable; generated identity depends on private proof.
        assert first.document_id in await control.topology.allowed_document_ids(
            "DEFAULT", access=ACCESS
        )
        assert await control.topology.active_mentions("DEFAULT", access=ACCESS) == ()
        privileged = AccessContext(tenant_id="DEFAULT", principal_id="privileged")
        assert len(await control.topology.active_mentions("DEFAULT", access=privileged)) == 2
        inputs = await control.topology.get_build_lineage("DEFAULT", remapped.build_id)
        assert inputs is not None and len(inputs.input_document_versions) == 2
        await control.publisher.retire_removed(document_id=str(private.document_id))
        assert await control.topology.active_mentions("DEFAULT", access=privileged) == ()


@pytest.mark.asyncio
async def test_concurrent_workers_share_one_durable_concurrency_slot(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.topology.configure_policy(policy())
        await publish(control)
        await control.topology.configure_indexing(
            TenantIndexingConfig(
                tenant_id="DEFAULT",
                enabled=True,
                budgets=IndexingBudgetLimits(max_concurrency=1),
            )
        )
        job = await control.topology.claim("DEFAULT")
        assert job is not None
        requests = tuple(
            BudgetRequest(
                reservation_id=f"worker-{index}",
                input_tokens=10,
                output_tokens=10,
                cost_usd=Decimal("0.01"),
            )
            for index in range(2)
        )
        admissions = await asyncio.gather(
            *(control.topology.reserve_budget(job, value) for value in requests)
        )
        assert sum(value.admitted for value in admissions) == 1
        assert {value.reason for value in admissions} == {None, "concurrency"}
