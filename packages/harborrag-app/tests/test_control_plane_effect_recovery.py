"""Secret retirement and activity logging must survive a failed first attempt.

Regression coverage for a coderabbit finding: create/update/delete_source
each follow a committed source write with secret retirement and/or activity
logging. Previously, a failure in either step raised straight out of the
use case even though the source write had already succeeded, leaving
orphaned secrets or a missing audit trail with nothing recorded for retry.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from app_test_control_plane import control_plane_app_service

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

    async def put(self, value: str) -> str:
        return await self.inner.put(value)

    async def resolve(self, ref: str) -> str:
        return await self.inner.resolve(ref)

    async def delete(self, ref: str) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("secrets backend unavailable")
        await self.inner.delete(ref)


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


@pytest.mark.asyncio
async def test_create_source_survives_a_failed_activity_log_and_queues_it_for_retry(
    project: Project,
) -> None:
    activity_repo = _FlakyActivityRepository(FakeActivityRepository())
    pending = FakePendingEffectRepository()
    service = control_plane_app_service(
        projects=[project], activity_repository=activity_repo, pending_effects=pending
    )

    response = await service.create_source(
        tenant_id="DEFAULT",
        project_id="proj-a",
        source_type="local",
        name="Docs",
        config={"source_path": "./docs"},
        schedule=None,
        actor="tester",
    )

    assert response.ok
    assert not activity_repo.inner.entries
    assert len(pending.effects) == 1
    effect = next(iter(pending.effects.values()))
    assert effect.kind == "log_activity"

    recovered = await service.recover_pending_control_plane_effects()

    assert recovered == 1
    assert not pending.effects
    assert len(activity_repo.inner.entries) == 1
    assert activity_repo.inner.entries[0].verb == "created"


@pytest.mark.asyncio
async def test_update_source_survives_failed_stale_secret_retirement_and_activity_log(
    project: Project,
) -> None:
    secrets = FakeSecrets()
    activity = FakeActivityRepository()
    service = control_plane_app_service(
        projects=[project], secrets=secrets, activity_repository=activity
    )
    created = await service.create_source(
        tenant_id="DEFAULT",
        project_id="proj-a",
        source_type="jira",
        name="AuTa board",
        config={"base_url": "https://example.atlassian.net", "token": "old-token"},
        schedule=None,
        actor="tester",
    )
    old_ref = created.data["source"].secret_refs[0]

    flaky_secrets = _FlakySecrets(secrets, fail_times=1)
    flaky_activity = _FlakyActivityRepository(activity, fail_times=1)
    pending = FakePendingEffectRepository()
    service = control_plane_app_service(
        projects=[project],
        sources=[created.data["source"]],
        secrets=flaky_secrets,
        activity_repository=flaky_activity,
        pending_effects=pending,
    )

    response = await service.update_source(
        created.data["source"].id,
        updates={"config": {"base_url": "https://example.atlassian.net", "token": "new-token"}},
        actor="tester",
    )

    assert response.ok
    assert old_ref in flaky_secrets.inner.values  # stale-ref delete failed, so it's still there
    kinds = {effect.kind for effect in pending.effects.values()}
    assert kinds == {"retire_secret", "log_activity"}

    recovered = await service.recover_pending_control_plane_effects()

    assert recovered == 2
    assert not pending.effects
    assert old_ref not in flaky_secrets.inner.values


@pytest.mark.asyncio
async def test_delete_source_survives_failed_secret_retirement_and_activity_log(
    project: Project,
) -> None:
    secrets = FakeSecrets()
    service = control_plane_app_service(projects=[project], secrets=secrets)
    created = await service.create_source(
        tenant_id="DEFAULT",
        project_id="proj-a",
        source_type="jira",
        name="AuTa board",
        config={"base_url": "https://example.atlassian.net", "token": "hunter2"},
        schedule=None,
        actor="tester",
    )
    source_id = created.data["source"].id
    ref = created.data["source"].secret_refs[0]

    flaky_secrets = _FlakySecrets(secrets, fail_times=1)
    flaky_activity = _FlakyActivityRepository(FakeActivityRepository(), fail_times=1)
    pending = FakePendingEffectRepository()
    service = control_plane_app_service(
        projects=[project],
        sources=[created.data["source"]],
        secrets=flaky_secrets,
        activity_repository=flaky_activity,
        pending_effects=pending,
    )

    response = await service.delete_source(source_id, actor="tester")

    assert response.ok
    assert ref in flaky_secrets.inner.values
    kinds = {effect.kind for effect in pending.effects.values()}
    assert kinds == {"retire_secret", "log_activity"}

    recovered = await service.recover_pending_control_plane_effects()

    assert recovered == 2
    assert ref not in flaky_secrets.inner.values
    assert len(flaky_activity.inner.entries) == 1


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


@pytest.mark.asyncio
async def test_a_failed_enqueue_is_logged_and_swallowed_not_raised(project: Project) -> None:
    """The primary write already committed -- even a double failure must not surface as an error."""
    activity_repo = _FlakyActivityRepository(FakeActivityRepository())
    service = control_plane_app_service(
        projects=[project],
        activity_repository=activity_repo,
        pending_effects=_AlwaysFailsPendingEffects(),
    )

    response = await service.create_source(
        tenant_id="DEFAULT",
        project_id="proj-a",
        source_type="local",
        name="Docs",
        config={"source_path": "./docs"},
        schedule=None,
        actor="tester",
    )

    assert response.ok
