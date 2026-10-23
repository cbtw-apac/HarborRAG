"""Legacy tenant-less secret retirements are replayed under DEFAULT, not dropped."""

from __future__ import annotations

import pytest
from app_test_control_plane import control_plane_app_service

from harborrag_core.domain.pending_effect import PendingControlPlaneEffect
from harborrag_core.testing.control_plane_fakes import FakePendingEffectRepository
from harborrag_core.testing.fakes import FakeSecrets


@pytest.mark.asyncio
async def test_a_retirement_queued_before_tenant_scoping_retires_the_default_secret() -> None:
    """R2-06: alpha1 queued retire_secret effects without a tenant.

    Migration 0032 leaves a secret no source still references under DEFAULT, so
    that is where the replay must delete it. Dropping the effect orphaned the
    ciphertext for good.
    """

    secrets = FakeSecrets()
    legacy = await secrets.put("old-token", tenant_id="DEFAULT")
    kept = await secrets.put("other-tenant-token", tenant_id="acme")
    pending = FakePendingEffectRepository()
    pending.effects["legacy"] = PendingControlPlaneEffect(
        id="legacy", kind="retire_secret", payload={"ref": legacy}
    )
    service = control_plane_app_service(secrets=secrets, pending_effects=pending)

    recovered = await service.recover_pending_control_plane_effects()

    assert recovered == 1
    assert pending.effects == {}
    assert ("DEFAULT", legacy) not in secrets.values
    assert secrets.values[("acme", kept)] == "other-tenant-token"
