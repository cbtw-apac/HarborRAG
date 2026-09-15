from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import update

from harborrag_adapters.repositories.database.ingestion_control.topology.schema import TOPOLOGY_JOBS
from harborrag_core.base import utc_now
from harborrag_core.contracts import HarborConflictError
from harborrag_core.topology import ChunkExtractionCheckpoint
from harborrag_core.topology.budget import IndexingBudgetLimits
from harborrag_core.topology.config import TenantIndexingConfig

from .ingestion_control_fixtures import candidate, make_control_plane
from .topology_fixtures import ACCESS, artifact, policy, prepared_build, publish


@pytest.mark.asyncio
async def test_opt_in_outbox_publication_and_unchanged_backfill(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        await publish(control)
        assert await control.topology.list_jobs("DEFAULT") == ()
        assert await control.topology.configure_policy(policy()) == 1
        assert await control.topology.configure_policy(policy()) == 1
        assert await control.topology.reconcile("DEFAULT") == 1
        assert await control.topology.reconcile("DEFAULT") == 0
        before = await control.topology.list_jobs("DEFAULT")
        version = candidate("one")
        await control.publisher.publish(
            document_id=str(version.document_id),
            candidate_document_version_id=str(version.document_version_id),
        )
        assert await control.topology.list_jobs("DEFAULT") == before
        await publish(control, "two")
        assert len(await control.topology.list_jobs("DEFAULT")) == 2


@pytest.mark.asyncio
async def test_failed_publish_cannot_enqueue_intent(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.topology.configure_policy(policy())
        version = candidate("pending")
        await control.document_versions.create_candidate(version)
        with pytest.raises(HarborConflictError):
            await control.publisher.publish(
                document_id=str(version.document_id),
                candidate_document_version_id=str(version.document_version_id),
            )
        assert await control.topology.list_jobs("DEFAULT") == ()


@pytest.mark.asyncio
async def test_staged_build_hidden_until_accept_and_removed_on_retirement(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.topology.configure_policy(policy())
        await publish(control)
        job = await control.topology.claim("DEFAULT")
        assert job is not None
        build = await prepared_build(control, job)
        await control.topology.stage(job, build)
        assert await control.topology.active_mentions("DEFAULT", access=ACCESS) == ()
        with pytest.raises(HarborConflictError, match="verified"):
            await control.topology.accept(job, build.build_id)
        await control.topology.mark_verified(job, build.build_id)
        assert await control.topology.accept(job, build.build_id)
        assert (
            len(await control.topology.active_mentions("DEFAULT", access=ACCESS, labels=("a",)))
            == 1
        )
        assert len(await control.topology.active_assertions("DEFAULT", access=ACCESS)) == 1
        assert await control.topology.eligible_build_ids(
            "DEFAULT", (build.build_id,), access=ACCESS
        ) == {build.build_id}
        assert await control.topology.get_build("DEFAULT", build.build_id) == build
        await control.publisher.retire_removed(document_id=job.document_id)
        assert await control.topology.active_assertions("DEFAULT", access=ACCESS) == ()
        assert (
            await control.topology.eligible_build_ids("DEFAULT", (build.build_id,), access=ACCESS)
            == set()
        )


@pytest.mark.asyncio
async def test_late_build_after_replacement_cannot_reactivate(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.topology.configure_policy(policy())
        await publish(control)
        job = await control.topology.claim("DEFAULT")
        assert job is not None
        build = await prepared_build(control, job)
        await control.topology.stage(job, build)
        await publish(control, "two")
        assert not await control.topology.accept(job, build.build_id)
        with pytest.raises(HarborConflictError):
            await control.topology.stage(job, build)
        assert await control.topology.active_assertions("DEFAULT", access=ACCESS) == ()


@pytest.mark.asyncio
async def test_disable_and_reenable_invalidates_accepted_build_and_old_worker(
    tmp_path: Path,
) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.topology.configure_policy(policy())
        await publish(control)
        job = await control.topology.claim("DEFAULT")
        assert job is not None
        build = await prepared_build(control, job)
        await control.topology.stage(job, build)
        await control.topology.mark_verified(job, build.build_id)
        assert await control.topology.accept(job, build.build_id)
        assert await control.topology.configure_policy(policy(enabled=False)) == 2
        assert await control.topology.active_mentions("DEFAULT", access=ACCESS) == ()
        assert await control.topology.configure_policy(policy()) == 3
        assert await control.topology.active_mentions("DEFAULT", access=ACCESS) == ()
        assert not await control.topology.accept(job, build.build_id)
        assert await control.topology.reconcile("DEFAULT") == 1
        current = await control.topology.claim("DEFAULT")
        assert current is not None and current.job_id != job.job_id


@pytest.mark.asyncio
async def test_expired_lease_fences_writes_and_retry_budget_is_bounded(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.topology.configure_policy(policy())
        await publish(control)
        await control.topology.configure_indexing(
            TenantIndexingConfig(
                tenant_id="DEFAULT",
                enabled=True,
                budgets=IndexingBudgetLimits(max_job_attempts=2),
            )
        )
        first = await control.topology.claim("DEFAULT")
        assert first is not None
        assert await control.topology.claim("DEFAULT") is None
        build = await prepared_build(control, first)
        async with control._client.sessions.begin() as session:
            await session.execute(
                update(TOPOLOGY_JOBS)
                .where(TOPOLOGY_JOBS.c.job_id == first.job_id)
                .values(lease_until=utc_now() - timedelta(seconds=1))
            )
        second = await control.topology.claim("DEFAULT")
        assert second is not None and second.fence > first.fence
        with pytest.raises(HarborConflictError):
            await control.topology.stage(first, build)
        await control.topology.fail(second, "provider_timeout")
        assert await control.topology.claim("DEFAULT") is None
        current = await control.topology.get_job("DEFAULT", first.job_id)
        assert current is not None and current.state == "failed" and current.attempts == 2
        await control.topology.configure_indexing(
            TenantIndexingConfig(
                tenant_id="DEFAULT",
                enabled=True,
                budgets=IndexingBudgetLimits(max_job_attempts=3),
            )
        )
        third = await control.topology.claim("DEFAULT")
        assert third is not None and third.attempts == 3


@pytest.mark.asyncio
async def test_checkpoint_immutability_and_complete_coverage(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.topology.configure_policy(policy())
        await publish(control)
        job = await control.topology.claim("DEFAULT")
        assert job is not None
        build = await prepared_build(control, job)
        with pytest.raises(HarborConflictError, match="immutable"):
            await control.topology.checkpoint(
                job,
                ChunkExtractionCheckpoint(
                    chunk_id="evidence-1",
                    input_digest="changed",
                    artifact=artifact(),
                    deployment_revision="r1",
                ),
            )
        with pytest.raises(HarborConflictError, match="coverage"):
            await control.topology.stage(
                job, build.model_copy(update={"chunk_ids": (), "mentions": (), "assertions": ()})
            )
        assert await control.topology.reusable_checkpoint(
            "DEFAULT", job.policy.profile.fingerprint, "input"
        )
        assert (
            await control.topology.reusable_checkpoint(
                "other", job.policy.profile.fingerprint, "input"
            )
            is None
        )


@pytest.mark.asyncio
async def test_tenant_isolation_at_every_read_and_claim_boundary(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.topology.configure_policy(policy())
        await publish(control)
        job = await control.topology.claim("DEFAULT")
        assert job is not None
        build = await prepared_build(control, job)
        await control.topology.stage(job, build)
        await control.topology.mark_verified(job, build.build_id)
        assert await control.topology.accept(job, build.build_id)
        assert await control.topology.claim("other", job_id=job.job_id) is None
        assert await control.topology.get_job("other", job.job_id) is None
        assert await control.topology.get_build("other", build.build_id) is None
        assert await control.topology.active_mentions("other") == ()
        assert await control.topology.active_assertions("other") == ()
        assert await control.topology.eligible_build_ids("other", (build.build_id,)) == set()
        assert (
            await control.topology.checkpoints(job.model_copy(update={"tenant_id": "other"})) == ()
        )
