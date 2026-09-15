from pathlib import Path

import pytest

from .ingestion_control_fixtures import make_control_plane
from .topology_fixtures import policy, prepared_build, publish


@pytest.mark.asyncio
async def test_cleanup_never_selects_a_live_staged_or_accepted_build(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.topology.configure_policy(policy())
        await publish(control)
        job = await control.topology.claim("DEFAULT")
        assert job is not None
        build = await prepared_build(control, job)
        await control.topology.stage(job, build)
        assert await control.topology.audit_build_ids("DEFAULT") == ()
        assert await control.topology.retired_build_ids("DEFAULT") == ()
        await control.topology.mark_verified(job, build.build_id)
        assert await control.topology.accept(job, build.build_id)
        assert await control.topology.audit_build_ids("DEFAULT") == (build.build_id,)
        assert (
            await control.topology.audit_build_ids("DEFAULT", after_build_id=build.build_id) == ()
        )
        assert await control.topology.retired_build_ids("DEFAULT") == ()
        await control.topology.configure_policy(policy(enabled=False))
        assert await control.topology.audit_build_ids("DEFAULT") == ()
        assert await control.topology.retired_build_ids("DEFAULT", build_ids=(build.build_id,)) == (
            build.build_id,
        )
        assert await control.topology.retired_build_ids("other", build_ids=(build.build_id,)) == ()
        await control.topology.configure_policy(policy())
        assert await control.topology.retired_build_ids("DEFAULT") == (build.build_id,)
        assert (
            await control.topology.retired_build_ids("DEFAULT", after_build_id=build.build_id) == ()
        )


@pytest.mark.asyncio
async def test_cleanup_selects_previous_fence_after_retry_but_not_current_candidate(
    tmp_path: Path,
) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.topology.configure_policy(policy())
        await publish(control)
        first = await control.topology.claim("DEFAULT")
        assert first is not None
        build = await prepared_build(control, first)
        await control.topology.stage(first, build)
        await control.topology.fail(first, "projection_timeout")
        assert await control.topology.retired_build_ids("DEFAULT") == ()
        second = await control.topology.claim("DEFAULT")
        assert second is not None
        retry_build = await prepared_build(control, second)
        await control.topology.stage(second, retry_build)
        assert await control.topology.retired_build_ids("DEFAULT") == (build.build_id,)
