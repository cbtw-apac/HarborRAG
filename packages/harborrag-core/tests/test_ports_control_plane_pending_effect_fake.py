"""Contract test for FakePendingEffectRepository.

Split out of test_ports_control_plane_fakes.py (file-length gate).
"""

from __future__ import annotations

import pytest

from harborrag_core.domain.pending_effect import PendingControlPlaneEffect
from harborrag_core.testing.control_plane_fakes import FakePendingEffectRepository

pytestmark = pytest.mark.whitebox


@pytest.mark.asyncio
async def test_fake_pending_effect_repository_is_oldest_first_and_completion_removes_it() -> None:
    repository = FakePendingEffectRepository()
    first = PendingControlPlaneEffect(id="eff-1", kind="retire_secret", payload={"ref": "s1"})
    second = PendingControlPlaneEffect(id="eff-2", kind="log_activity", payload={"id": "act-1"})
    await repository.enqueue(first)
    await repository.enqueue(second)

    assert [effect.id for effect in await repository.list_pending()] == ["eff-1", "eff-2"]
    assert [effect.id for effect in await repository.list_pending(limit=1)] == ["eff-1"]

    await repository.complete("eff-1")
    assert [effect.id for effect in await repository.list_pending()] == ["eff-2"]
    await repository.complete("eff-1")  # already gone: a no-op, not an error
    assert [effect.id for effect in await repository.list_pending()] == ["eff-2"]
