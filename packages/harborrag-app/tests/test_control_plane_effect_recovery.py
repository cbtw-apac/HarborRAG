"""Secret retirement and activity logging must survive a failed first attempt.

Connection and project writes follow a committed row write with secret
retirement and activity logging through ``retire_refs`` and ``log_activity``.
A failure in either step must be queued for retry, never raised, because the
row write already succeeded.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from app_test_control_plane import control_plane_app_service

from harborrag_app.workflow_control.control_plane.effect_recovery import log_activity, retire_refs
from harborrag_core.domain.activity import ActivityEntry
from harborrag_core.domain.pending_effect import PendingControlPlaneEffect
from harborrag_core.domain.project import Project
from harborrag_core.testing.control_plane_fakes import (
    FakeActivityRepository,
    FakePendingEffectRepository,
)
from harborrag_core.testing.fakes import FakeSecrets


@dataclass(slots=True)
class _FlakyActivityRepository:
    """Wraps a real fake; its first ``fail_times`` appends raise."""

    inner: FakeActivityRepository
    fail_times: int = 1

    async def append(self, entry: ActivityEntry) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("activity backend unavailable")
        await self.inner.append(entry)

    async def list(self, limit: int = 50, *, tenant_ids=None):
        return await self.inner.list(limit, tenant_ids=tenant_ids)


@dataclass(slots=True)
class _FlakySecrets:
    """Wraps a real fake; its first ``fail_times`` deletes raise."""

    inner: FakeSecrets
    fail_times: int = 1

    async def put(self, value: str, *, tenant_id: str) -> str:
        return await self.inner.put(value, tenant_id=tenant_id)

    async def resolve(self, ref: str, *, tenant_id: str) -> str:
        return await self.inner.resolve(ref, tenant_id=tenant_id)

    async def delete(self, ref: str, *, tenant_id: str) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("secrets backend unavailable")
        await self.inner.delete(ref, tenant_id=tenant_id)


@dataclass(slots=True)
class _AlwaysFailsPendingEffects:
    """Enqueue itself fails -- the last-resort case where the effect is truly lost."""

    effects: dict[str, PendingControlPlaneEffect] = field(default_factory=dict)

    async def enqueue(self, effect: PendingControlPlaneEffect) -> None:
        raise RuntimeError("pending-effect store unavailable")

    async def list_pending(self, *, limit: int = 100) -> list[PendingControlPlaneEffect]:
        return list(self.effects.values())[:limit]

    async def complete(self, effect_id: str) -> None:
        self.effects.pop(effect_id, None)


@pytest.fixture
def project() -> Project:
    return Project(id="proj-a", name="A", collection="a", tenant_id="DEFAULT")


def _entry() -> ActivityEntry:
    return ActivityEntry(
        id="act_1",
        tenant_id="DEFAULT",
        actor="tester",
        verb="created",
        entity_type="connection",
        entity_id="docs",
        summary="Created connection 'Docs'",
    )


@pytest.mark.asyncio
async def test_a_failed_activity_log_is_queued_and_replayed(project: Project) -> None:
    activity_repo = _FlakyActivityRepository(FakeActivityRepository())
    pending = FakePendingEffectRepository()
    service = control_plane_app_service(
        projects=[project], activity_repository=activity_repo, pending_effects=pending
    )

    await log_activity(service._control_plane(), _entry())

    assert not activity_repo.inner.entries
    assert [effect.kind for effect in pending.effects.values()] == ["log_activity"]

    recovered = await service.recover_pending_control_plane_effects()

    assert recovered == 1
    assert not pending.effects
    assert [entry.id for entry in activity_repo.inner.entries] == ["act_1"]


@pytest.mark.asyncio
async def test_a_failed_secret_retirement_is_queued_and_replayed(project: Project) -> None:
    secrets = FakeSecrets()
    ref = await secrets.put("hunter2", tenant_id="DEFAULT")
    flaky_secrets = _FlakySecrets(secrets, fail_times=1)
    pending = FakePendingEffectRepository()
    service = control_plane_app_service(
        projects=[project], secrets=flaky_secrets, pending_effects=pending
    )

    await retire_refs(service._control_plane(), [ref], tenant_id="DEFAULT")

    assert ("DEFAULT", ref) in secrets.values
    assert [effect.kind for effect in pending.effects.values()] == ["retire_secret"]

    recovered = await service.recover_pending_control_plane_effects()

    assert recovered == 1
    assert not pending.effects
    assert ("DEFAULT", ref) not in secrets.values


@pytest.mark.asyncio
async def test_recovery_leaves_a_repeatedly_failing_effect_pending_without_blocking_others(
    project: Project,
) -> None:
    activity_repo = FakeActivityRepository()
    pending = FakePendingEffectRepository()
    pending.effects["good"] = PendingControlPlaneEffect(
        id="good",
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
    pending.effects["bad"] = PendingControlPlaneEffect(
        id="bad", kind="unknown_kind_from_a_future_version", payload={}
    )
    service = control_plane_app_service(
        projects=[project], activity_repository=activity_repo, pending_effects=pending
    )

    recovered = await service.recover_pending_control_plane_effects()

    assert recovered == 1
    assert set(pending.effects) == {"bad"}
    assert len(activity_repo.entries) == 1


@dataclass(slots=True)
class _CompleteFailsOnce:
    """Wraps a real fake; ``complete`` raises for one chosen effect id, once."""

    inner: FakePendingEffectRepository
    fails_for: str

    async def enqueue(self, effect: PendingControlPlaneEffect) -> None:
        await self.inner.enqueue(effect)

    async def list_pending(self, *, limit: int = 100) -> list[PendingControlPlaneEffect]:
        return await self.inner.list_pending(limit=limit)

    async def complete(self, effect_id: str) -> None:
        if effect_id == self.fails_for:
            self.fails_for = ""
            raise RuntimeError("pending-effect store unavailable")
        await self.inner.complete(effect_id)


@pytest.mark.asyncio
async def test_a_completion_failure_does_not_block_the_rest_of_the_batch(
    project: Project,
) -> None:
    """A replay can succeed while the follow-up ``complete`` call fails (e.g. a
    transient DB error). That row simply stays pending for the next drain
    pass -- both effect kinds are safe to replay again -- but the failure
    must not abort processing of the other pending rows in this same pass."""
    activity_repo = FakeActivityRepository()
    inner = FakePendingEffectRepository()
    inner.effects["fails-to-complete"] = PendingControlPlaneEffect(
        id="fails-to-complete",
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
    inner.effects["completes-fine"] = PendingControlPlaneEffect(
        id="completes-fine",
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
    pending = _CompleteFailsOnce(inner, fails_for="fails-to-complete")
    service = control_plane_app_service(
        projects=[project], activity_repository=activity_repo, pending_effects=pending
    )

    recovered = await service.recover_pending_control_plane_effects()

    assert recovered == 1
    assert set(inner.effects) == {"fails-to-complete"}
    assert {entry.id for entry in activity_repo.entries} == {"act_1", "act_2"}


@pytest.mark.asyncio
async def test_a_failed_enqueue_is_logged_and_swallowed_not_raised(project: Project) -> None:
    """The primary write already committed -- even a double failure must not surface as an error."""
    activity_repo = _FlakyActivityRepository(FakeActivityRepository())
    service = control_plane_app_service(
        projects=[project],
        activity_repository=activity_repo,
        pending_effects=_AlwaysFailsPendingEffects(),
    )

    await log_activity(service._control_plane(), _entry())

    assert not activity_repo.inner.entries
