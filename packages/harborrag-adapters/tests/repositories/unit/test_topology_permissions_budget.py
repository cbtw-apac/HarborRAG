from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, update

from harborrag_adapters.repositories.database.ingestion_control.topology.policy_schema import (
    BUDGET_DAYS,
)
from harborrag_adapters.repositories.database.ingestion_control.topology.schema import TOPOLOGY_JOBS
from harborrag_core.base import utc_now
from harborrag_core.contracts import HarborConflictError
from harborrag_core.security.context import AccessContext
from harborrag_core.topology.budget import BudgetRequest, IndexingBudgetLimits, UsageSettlement
from harborrag_core.topology.config import TenantIndexingConfig
from harborrag_core.topology.permissions import DerivedArtifactLineage, ResolvedPermissionSnapshot

from .ingestion_control_fixtures import advance_to_verified, candidate, make_control_plane
from .topology_fixtures import ACCESS, artifact, permit, policy, prepared_build, publish


def acl(resource_id: str, **changes: object) -> ResolvedPermissionSnapshot:
    now = utc_now()
    values = {
        "tenant_id": "DEFAULT",
        "resource_kind": "document",
        "resource_id": resource_id,
        "revision": "acl-2",
        "resolved_at": now,
        "expires_at": now + timedelta(hours=1),
        "known": True,
        "processing_allowed": True,
        "public": True,
    }
    values.update(changes)
    return ResolvedPermissionSnapshot.model_validate(values)


@pytest.mark.asyncio
async def test_default_off_prohibition_and_unknown_permissions_are_separate(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        version = candidate("one")
        await control.topology.configure_policy(policy())
        await advance_to_verified(control, version)
        await control.publisher.publish(
            document_id=str(version.document_id),
            candidate_document_version_id=str(version.document_version_id),
        )
        assert not (await control.topology.get_indexing("DEFAULT")).config.enabled
        assert await control.topology.reconcile("DEFAULT") == 0
        await control.topology.configure_indexing(
            TenantIndexingConfig(tenant_id="DEFAULT", enabled=True, prohibited=True)
        )
        assert await control.topology.reconcile("DEFAULT") == 0
        await control.topology.configure_indexing(
            TenantIndexingConfig(tenant_id="DEFAULT", enabled=True)
        )
        assert await control.topology.reconcile("DEFAULT") == 1
        assert await control.topology.reconcile("DEFAULT") == 0
        assert await control.topology.claim("DEFAULT") is None
        assert (await control.topology.list_jobs("DEFAULT"))[0].state == "deferred"
        await permit(control, str(version.document_id))
        assert await control.topology.reconcile("DEFAULT") == 1
        assert await control.topology.claim("DEFAULT") is not None


@pytest.mark.asyncio
async def test_acl_filtered_before_reads_and_mode_off_preserves_raw_acl(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.topology.configure_policy(policy())
        await publish(control)
        job = await control.topology.claim("DEFAULT")
        assert job is not None
        build = await prepared_build(control, job)
        await control.topology.stage(job, build)
        await control.topology.mark_verified(job, build.build_id)
        assert await control.topology.accept(job, build.build_id)
        assert await control.topology.active_mentions("DEFAULT") == ()
        assert len(await control.topology.active_mentions("DEFAULT", access=ACCESS)) == 2
        assert await control.topology.allowed_document_ids("DEFAULT", access=ACCESS) == (
            job.document_id,
        )
        state = await control.topology.get_indexing("DEFAULT")
        paused = await control.topology.configure_indexing(
            state.config.model_copy(update={"spending_paused": True})
        )
        assert paused.epoch == state.epoch
        assert len(await control.topology.active_mentions("DEFAULT", access=ACCESS)) == 2
        assert await control.topology.retired_build_ids("DEFAULT") == ()
        await control.topology.configure_indexing(TenantIndexingConfig(tenant_id="DEFAULT"))
        assert await control.topology.active_mentions("DEFAULT", access=ACCESS) == ()
        assert await control.topology.retired_build_ids("DEFAULT") == (build.build_id,)
        assert await control.topology.allowed_document_ids("DEFAULT", access=ACCESS) == (
            job.document_id,
        )
        await control.topology.set_permissions(
            acl(job.document_id, denied_principal_ids=(ACCESS.principal_id,))
        )
        assert await control.topology.allowed_document_ids("DEFAULT", access=ACCESS) == ()
        other = AccessContext(principal_id="reader", tenant_id="DEFAULT")
        assert await control.topology.allowed_document_ids("DEFAULT", access=other) == (
            job.document_id,
        )


@pytest.mark.asyncio
async def test_permission_revision_change_fences_worker_and_rejects_aba(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.topology.configure_policy(policy())
        await publish(control)
        job = await control.topology.claim("DEFAULT")
        assert job is not None
        build = await prepared_build(control, job)
        await control.topology.stage(job, build)
        await control.topology.set_permissions(acl(job.document_id))
        assert not await control.topology.accept(job, build.build_id)
        assert await control.topology.retired_build_ids("DEFAULT") == (build.build_id,)
        with pytest.raises(HarborConflictError, match="retired"):
            await control.topology.set_permissions(acl(job.document_id, revision="acl-1"))
        with pytest.raises(HarborConflictError, match="new revision"):
            await control.topology.set_permissions(acl(job.document_id, public=False))
        assert await control.topology.reconcile("DEFAULT") == 1
        current = await control.topology.claim("DEFAULT")
        assert current is not None and current.job_id != job.job_id


@pytest.mark.asyncio
async def test_reservations_charge_missing_usage_bound_calls_and_refund_only_known(
    tmp_path: Path,
) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.topology.configure_policy(policy())
        await publish(control)
        await control.topology.configure_indexing(
            TenantIndexingConfig(
                tenant_id="DEFAULT",
                enabled=True,
                budgets=IndexingBudgetLimits(
                    max_concurrency=1, daily_token_cap=250, daily_cost_usd=Decimal("1")
                ),
            )
        )
        job = await control.topology.claim("DEFAULT")
        assert job is not None
        request = BudgetRequest(
            reservation_id="r1",
            input_tokens=60,
            output_tokens=40,
            cost_usd=Decimal("0.1"),
            operation_key="chunk",
            provider_calls=2,
            max_provider_calls=4,
        )
        assert (await control.topology.reserve_budget(job, request)).admitted
        assert (await control.topology.reserve_budget(job, request)).reason == "reservation_exists"
        second = request.model_copy(update={"reservation_id": "r2"})
        assert (await control.topology.reserve_budget(job, second)).reason == "concurrency"
        await control.topology.settle_budget("DEFAULT", "r1", UsageSettlement())
        assert (await control.topology.reserve_budget(job, second)).admitted
        await control.topology.settle_budget(
            "DEFAULT",
            "r2",
            UsageSettlement(input_tokens=20, output_tokens=10, cost_usd=Decimal("0.02")),
        )
        assert (
            await control.topology.reserve_budget(
                job, request.model_copy(update={"reservation_id": "r3"})
            )
        ).reason == "operation_call_cap"
        async with control._client.sessions() as session:
            row = (await session.execute(select(BUDGET_DAYS))).mappings().one()
            assert row["tokens"] == 130 and row["cost_microusd"] == 120000
        with pytest.raises(HarborConflictError, match="immutable"):
            await control.topology.settle_budget("DEFAULT", "r1", UsageSettlement(input_tokens=0))
        costly = request.model_copy(
            update={"reservation_id": "cost", "operation_key": "other", "cost_usd": Decimal("1")}
        )
        assert (await control.topology.reserve_budget(job, costly)).reason == "daily_cost"
        costly = costly.model_copy(update={"cost_usd": Decimal("0.1"), "input_tokens": 1000})
        assert (await control.topology.reserve_budget(job, costly)).reason == "daily_tokens"


@pytest.mark.asyncio
async def test_pre_dispatch_deferral_does_not_burn_attempt_and_disable_fences_budget(
    tmp_path: Path,
) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.topology.configure_policy(policy())
        await publish(control)
        job = await control.topology.claim("DEFAULT")
        assert job is not None
        await control.topology.defer_job(job, "busy", utc_now() + timedelta(seconds=1))
        current = await control.topology.get_job("DEFAULT", job.job_id)
        assert current is not None and current.attempts == 0 and current.state == "deferred"
        async with control._client.sessions.begin() as session:
            await session.execute(
                update(TOPOLOGY_JOBS).values(available_at=utc_now() - timedelta(seconds=1))
            )
        job = await control.topology.claim("DEFAULT")
        assert job is not None and job.attempts == 1
        await control.topology.configure_indexing(TenantIndexingConfig(tenant_id="DEFAULT"))
        with pytest.raises(HarborConflictError):
            await control.topology.reserve_budget(
                job,
                BudgetRequest(
                    reservation_id="off", input_tokens=1, output_tokens=1, cost_usd=Decimal("0")
                ),
            )


@pytest.mark.asyncio
async def test_operation_call_cap_advances_to_the_next_bounded_job_attempt(
    tmp_path: Path,
) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.topology.configure_policy(policy())
        await publish(control)
        await control.topology.configure_indexing(
            TenantIndexingConfig(
                tenant_id="DEFAULT",
                enabled=True,
                budgets=IndexingBudgetLimits(
                    max_concurrency=1,
                    daily_token_cap=1000,
                    daily_cost_usd=Decimal("1"),
                ),
            )
        )
        first = await control.topology.claim("DEFAULT")
        assert first is not None and first.attempts == 1
        request = BudgetRequest(
            reservation_id="attempt-one",
            input_tokens=1,
            output_tokens=1,
            cost_usd=Decimal("0.01"),
            operation_key="chunk-attempt-one",
        )
        assert (await control.topology.reserve_budget(first, request)).admitted
        await control.topology.settle_budget("DEFAULT", request.reservation_id, UsageSettlement())
        await control.topology.defer_job(first, "operation_call_cap")

        second = await control.topology.claim("DEFAULT")

        assert second is not None
        assert second.job_id == first.job_id
        assert second.attempts == 2


@pytest.mark.asyncio
async def test_old_fence_reservation_retains_charge_without_blocking_concurrency(
    tmp_path: Path,
) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.topology.configure_policy(policy())
        await publish(control)
        await control.topology.configure_indexing(
            TenantIndexingConfig(
                tenant_id="DEFAULT",
                enabled=True,
                budgets=IndexingBudgetLimits(
                    max_concurrency=1,
                    daily_token_cap=1000,
                    daily_cost_usd=Decimal("1"),
                ),
            )
        )
        first = await control.topology.claim("DEFAULT")
        assert first is not None
        first_request = BudgetRequest(
            reservation_id="old-fence",
            input_tokens=10,
            output_tokens=10,
            cost_usd=Decimal("0.01"),
            operation_key="old-operation",
        )
        assert (await control.topology.reserve_budget(first, first_request)).admitted
        async with control._client.sessions.begin() as session:
            await session.execute(
                update(TOPOLOGY_JOBS).values(lease_until=utc_now() - timedelta(seconds=1))
            )
        second = await control.topology.claim("DEFAULT")
        assert second is not None and second.fence > first.fence
        second_request = first_request.model_copy(
            update={
                "reservation_id": "current-fence",
                "operation_key": "current-operation",
            }
        )

        assert (await control.topology.reserve_budget(second, second_request)).admitted
        async with control._client.sessions() as session:
            ledger = (await session.execute(select(BUDGET_DAYS))).mappings().one()
        assert ledger["tokens"] == 40
        assert ledger["cost_microusd"] == 20_000


@pytest.mark.asyncio
async def test_more_permissive_budget_wakes_only_matching_deferred_jobs(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.topology.configure_policy(policy())
        await publish(control)
        initial = TenantIndexingConfig(
            tenant_id="DEFAULT",
            enabled=True,
            budgets=IndexingBudgetLimits(
                max_concurrency=1,
                daily_token_cap=10,
                daily_cost_usd=Decimal("1"),
            ),
        )
        await control.topology.configure_indexing(initial)
        job = await control.topology.claim("DEFAULT")
        assert job is not None
        await control.topology.defer_job(
            job,
            "daily_tokens",
            utc_now() + timedelta(days=1),
        )
        assert (await control.topology.get_job("DEFAULT", job.job_id)).state == "deferred"

        # Idempotent reapplication must preserve the original backoff.
        await control.topology.configure_indexing(initial)
        assert (await control.topology.get_job("DEFAULT", job.job_id)).state == "deferred"

        expanded = initial.model_copy(
            update={"budgets": initial.budgets.model_copy(update={"daily_token_cap": 100})}
        )
        await control.topology.configure_indexing(expanded)
        ready = await control.topology.get_job("DEFAULT", job.job_id)
        assert ready is not None
        assert ready.state == "pending"
        assert ready.available_at is None
        assert ready.error_code is None
        assert ready.attempts == 0


@pytest.mark.asyncio
async def test_post_accept_derivations_preserve_lineage_and_deny_acl_change(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.topology.configure_policy(policy())
        await publish(control)
        job = await control.topology.claim("DEFAULT")
        assert job is not None
        build = await prepared_build(control, job)
        await control.topology.stage(job, build)
        await control.topology.mark_verified(job, build.build_id)
        assert await control.topology.accept(job, build.build_id)
        inputs = await control.topology.get_build_lineage("DEFAULT", build.build_id)
        assert inputs is not None
        lineage = DerivedArtifactLineage(
            **inputs.model_dump(),
            artifact_id="description-1",
            artifact_kind="parent_description",
            build_id=build.build_id,
            input_digest="inputs",
            metadata={"point_ids": ["point"]},
        )
        await control.topology.publish_derivation("DEFAULT", lineage, artifact())
        await control.topology.publish_derivation("DEFAULT", lineage, artifact())
        retained = await control.topology.derivations_for_build("DEFAULT", build.build_id)
        assert len(retained) == 1 and retained[0].lineage == lineage
        assert await control.topology.active_derivations("DEFAULT") == ()
        assert len(await control.topology.active_derivations("DEFAULT", access=ACCESS)) == 1
        assert await control.topology.eligible_artifact_ids(
            "DEFAULT", ("description-1",), access=ACCESS
        ) == {"description-1"}
        with pytest.raises(HarborConflictError, match="all accepted"):
            await control.topology.publish_derivation(
                "DEFAULT",
                lineage.model_copy(update={"input_document_versions": {"hidden": "version"}}),
                artifact(),
            )
        await control.topology.set_permissions(acl(job.document_id))
        assert await control.topology.active_derivations("DEFAULT", access=ACCESS) == ()
        assert len(await control.topology.derivations_for_build("DEFAULT", build.build_id)) == 1
        assert (
            await control.topology.eligible_artifact_ids(
                "DEFAULT", ("description-1",), access=ACCESS
            )
            == set()
        )
