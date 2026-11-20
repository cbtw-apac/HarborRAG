from __future__ import annotations

import pytest

from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_adapters.repositories.database.control_plane.setting_overrides import (
    SqlSettingOverrideRepository,
)
from harborrag_adapters.repositories.database.control_plane.setting_patches import (
    SqlSettingPatchRepository,
)
from harborrag_adapters.repositories.database.control_plane.setting_scope_revisions import (
    SqlSettingScopeRevisionRepository,
)
from harborrag_core.contracts.errors import HarborPreconditionFailedError
from harborrag_core.domain.setting_override import SettingOverride

pytestmark = pytest.mark.asyncio


def _override(**kwargs: object) -> SettingOverride:
    defaults: dict[str, object] = {
        "tenant_id": "acme",
        "scope_level": "tenant",
        "scope_id": "acme",
        "key": "chunking.max_tokens",
        "value": 512,
        "created_by": "tester",
        "updated_by": "tester",
    }
    defaults.update(kwargs)
    return SettingOverride(**defaults)  # type: ignore[arg-type]


async def test_apply_bumps_the_revision_and_writes_the_override(
    sessions: SessionFactory,
) -> None:
    patches = SqlSettingPatchRepository(sessions)
    overrides = SqlSettingOverrideRepository(sessions)
    revisions = SqlSettingScopeRevisionRepository(sessions)

    new_revision = await patches.apply(
        "acme",
        "tenant",
        "acme",
        expected_revision=0,
        source="api",
        actor="u_huy",
        puts=(_override(),),
        deletes=(),
    )

    assert new_revision == 1
    assert await revisions.get("acme", "tenant", "acme") == 1
    assert await overrides.get_at("acme", "tenant", "acme") == {"chunking.max_tokens": 512}


async def test_apply_rejects_a_stale_revision_and_writes_nothing(
    sessions: SessionFactory,
) -> None:
    patches = SqlSettingPatchRepository(sessions)
    overrides = SqlSettingOverrideRepository(sessions)
    revisions = SqlSettingScopeRevisionRepository(sessions)
    await patches.apply(
        "acme",
        "tenant",
        "acme",
        expected_revision=0,
        source="api",
        actor="u_huy",
        puts=(_override(),),
        deletes=(),
    )

    with pytest.raises(HarborPreconditionFailedError):
        await patches.apply(
            "acme",
            "tenant",
            "acme",
            expected_revision=0,  # stale: the scope is already at 1
            source="api",
            actor="u_huy",
            puts=(_override(value=256),),
            deletes=(),
        )

    # Neither the revision nor the override moved past the first, successful call.
    assert await revisions.get("acme", "tenant", "acme") == 1
    assert await overrides.get_at("acme", "tenant", "acme") == {"chunking.max_tokens": 512}


async def test_apply_deletes_and_puts_together_in_one_call(
    sessions: SessionFactory,
) -> None:
    patches = SqlSettingPatchRepository(sessions)
    overrides = SqlSettingOverrideRepository(sessions)
    await patches.apply(
        "acme",
        "tenant",
        "acme",
        expected_revision=0,
        source="api",
        actor="u_huy",
        puts=(_override(), _override(key="parsing.ocr.enabled", value=True)),
        deletes=(),
    )

    new_revision = await patches.apply(
        "acme",
        "tenant",
        "acme",
        expected_revision=1,
        source="api",
        actor="u_huy",
        puts=(_override(value=256),),
        deletes=("parsing.ocr.enabled",),
    )

    assert new_revision == 2
    assert await overrides.get_at("acme", "tenant", "acme") == {"chunking.max_tokens": 256}
