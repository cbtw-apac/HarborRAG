"""AppService.recover_pending_control_plane_effects must not double-drain across processes.

Regression coverage for a review finding: recover_pending_control_plane_effects
read, replayed, and completed each pending effect with no claim or lease, so
two API processes/replicas draining the same queue on their own timers could
both replay the same row concurrently -- harmless for retire_secret (an
idempotent delete) but a double-logged audit entry for log_activity. The fix
gates the drain behind the same DB-backed lease pattern already used by the
ingestion progress bridge; this proves two AppService instances sharing one
lease repository (i.e. two processes against the same database) never both
drain in the same round.
"""

from __future__ import annotations

import pytest
from app_test_control_plane import control_plane_app_service

from harborrag_core.domain.pending_effect import PendingControlPlaneEffect
from harborrag_core.domain.project import Project
from harborrag_core.testing.control_plane_fakes import (
    FakeActivityRepository,
    FakeLeaseRepository,
    FakePendingEffectRepository,
)


def _pending_activity_effect() -> PendingControlPlaneEffect:
    return PendingControlPlaneEffect(
        id="eff_1",
        kind="log_activity",
        payload={
            "id": "act_1",
            "actor": "tester",
            "verb": "created",
            "entity_type": "source",
            "entity_id": "src_1",
            "summary": "Created source 'Docs'",
            "tenant_id": "DEFAULT",
            "created_at": "2026-08-12T00:00:00+00:00",
        },
    )


@pytest.mark.asyncio
async def test_only_the_lease_holder_drains_when_two_processes_share_a_database() -> None:
    project = Project(id="proj-a", name="A", collection="a", tenant_id="DEFAULT")
    lease_repository = FakeLeaseRepository()
    pending = FakePendingEffectRepository()
    pending.effects["eff_1"] = _pending_activity_effect()
    activity = FakeActivityRepository()

    process_a = control_plane_app_service(
        projects=[project],
        activity_repository=activity,
        pending_effects=pending,
        leases=lease_repository,
    )
    process_b = control_plane_app_service(
        projects=[project],
        activity_repository=activity,
        pending_effects=pending,
        leases=lease_repository,
    )

    recovered_a = await process_a.recover_pending_control_plane_effects()
    recovered_b = await process_b.recover_pending_control_plane_effects()

    assert (recovered_a, recovered_b) == (1, 0)
    assert not pending.effects
    assert [entry.id for entry in activity.entries] == ["act_1"]


@pytest.mark.asyncio
async def test_the_lease_fails_over_once_the_holder_stops_renewing() -> None:
    """A process that stops draining (crash, restart) must not permanently
    starve every other instance of the lease."""
    project = Project(id="proj-a", name="A", collection="a", tenant_id="DEFAULT")
    lease_repository = FakeLeaseRepository()
    pending = FakePendingEffectRepository()
    pending.effects["eff_1"] = _pending_activity_effect()
    activity = FakeActivityRepository()

    process_a = control_plane_app_service(
        projects=[project],
        activity_repository=activity,
        pending_effects=pending,
        leases=lease_repository,
    )
    process_b = control_plane_app_service(
        projects=[project],
        activity_repository=activity,
        pending_effects=pending,
        leases=lease_repository,
    )

    # a acquires the lease but its replay of this effect fails, so nothing
    # is recovered yet -- the point here is just that a becomes the holder.
    assert await process_a.recover_pending_control_plane_effects() == 1
    # a's lease lapses (crash / restart / a machine clock rolling forward past ttl):
    lease_repository.leases.clear()
    pending.effects["eff_2"] = PendingControlPlaneEffect(
        id="eff_2",
        kind="log_activity",
        payload={
            "id": "act_2",
            "actor": "tester",
            "verb": "created",
            "entity_type": "source",
            "entity_id": "src_2",
            "summary": "Created source 'Sheets'",
            "tenant_id": "DEFAULT",
            "created_at": "2026-08-12T00:00:01+00:00",
        },
    )

    assert await process_b.recover_pending_control_plane_effects() == 1  # b takes over
