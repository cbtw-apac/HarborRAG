import asyncio
from pathlib import Path

import pytest

from harborrag_core.contracts import HarborConflictError
from harborrag_core.topology import DocumentTopologyBuild

from .ingestion_control_fixtures import make_control_plane
from .topology_fixtures import ACCESS, artifact, policy, prepared_build, publish


@pytest.mark.asyncio
async def test_concurrent_policy_configurations_and_dispatch_claims_are_idempotent(
    tmp_path: Path,
) -> None:
    async with make_control_plane(tmp_path) as control:
        revisions = await asyncio.gather(
            *(control.topology.configure_policy(policy()) for _ in range(3))
        )
        assert revisions == [1, 1, 1]
        await publish(control)
        claims = await asyncio.gather(*(control.topology.claim("DEFAULT") for _ in range(3)))
        assert sum(claim is not None for claim in claims) == 1
        current = (await control.topology.list_jobs("DEFAULT"))[0]
        assert current.state == "running" and current.attempts == 1


@pytest.mark.asyncio
async def test_changed_build_records_cannot_reuse_unchanged_artifact_reference(
    tmp_path: Path,
) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.topology.configure_policy(policy())
        await publish(control)
        job = await control.topology.claim("DEFAULT")
        assert job is not None
        build = await prepared_build(control, job)
        await control.topology.stage(job, build)
        changed = build.model_copy(update={"assertions": ()})
        with pytest.raises(HarborConflictError, match="immutable"):
            await control.topology.stage(job, changed)


@pytest.mark.asyncio
async def test_build_rejects_cross_tenant_mentions_and_unsupported_assertion_endpoints(
    tmp_path: Path,
) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.topology.configure_policy(policy())
        await publish(control)
        job = await control.topology.claim("DEFAULT")
        assert job is not None
        build = await prepared_build(control, job)
        wrong_owner = build.mentions[0].model_copy(update={"tenant_id": "other"})
        with pytest.raises(ValueError, match="ownership"):
            await control.topology.stage(job, build.model_copy(update={"mentions": (wrong_owner,)}))
        unsupported = build.assertions[0].model_copy(update={"subject_entity_id": "unknown"})
        with pytest.raises(ValueError, match="endpoint"):
            await control.topology.stage(
                job, build.model_copy(update={"assertions": (unsupported,)})
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_chunks", [False, True])
async def test_zero_fact_and_zero_chunk_builds_are_explicit_successes(
    tmp_path: Path, empty_chunks: bool
) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.topology.configure_policy(policy())
        await publish(control)
        job = await control.topology.claim("DEFAULT")
        assert job is not None
        if empty_chunks:
            await control.topology.prepare(job, ())
            zero_facts = DocumentTopologyBuild(
                build_id="empty", job_id=job.job_id, artifact=artifact(), chunk_ids=()
            )
        else:
            build = await prepared_build(control, job)
            zero_facts = build.model_copy(update={"mentions": (), "assertions": ()})
        await control.topology.stage(job, zero_facts)
        await control.topology.mark_verified(job, zero_facts.build_id)
        assert await control.topology.accept(job, zero_facts.build_id)
        assert await control.topology.active_mentions("DEFAULT", access=ACCESS) == ()
        assert await control.topology.runnable_jobs("DEFAULT") == ()
