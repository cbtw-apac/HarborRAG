from __future__ import annotations

import pytest
import sqlalchemy as sa

from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_adapters.repositories.database.control_plane.setting_overrides import (
    SqlSettingOverrideRepository,
)
from harborrag_adapters.repositories.database.control_plane.setting_scope_revisions import (
    SqlSettingScopeRevisionRepository,
)
from harborrag_adapters.repositories.database.control_plane.settings_seed_repository import (
    SqlSettingsSeedRepository,
)
from harborrag_adapters.repositories.database.control_plane.workspace import SqlSettingsRepository
from harborrag_core.contracts.errors import HarborConflictError
from harborrag_core.domain.setting_override import SettingOverride

pytestmark = pytest.mark.asyncio


def _override(**kwargs: object) -> SettingOverride:
    defaults: dict[str, object] = {
        "tenant_id": "acme",
        "scope_level": "tenant",
        "scope_id": "acme",
        "key": "chunking.max_tokens",
        "value": 512,
        "created_by": "seed",
        "updated_by": "seed",
    }
    defaults.update(kwargs)
    return SettingOverride(**defaults)  # type: ignore[arg-type]


async def test_seed_writes_overrides_revision_and_marker_together(
    sessions: SessionFactory,
) -> None:
    seeder = SqlSettingsSeedRepository(sessions)
    overrides = SqlSettingOverrideRepository(sessions)
    revisions = SqlSettingScopeRevisionRepository(sessions)
    workspace = SqlSettingsRepository(sessions)

    await seeder.seed(
        tenant_id="acme",
        overrides=(_override(), _override(key="parsing.ocr.enabled", value=False)),
        workspace_tenant_id="DEFAULT",
        marker_key="settings_yaml_seed",
        marker_value={"path": "config.yaml", "hash": "abc"},
    )

    assert await overrides.get_at("acme", "tenant", "acme") == {
        "chunking.max_tokens": 512,
        "parsing.ocr.enabled": False,
    }
    assert await revisions.get("acme", "tenant", "acme") == 1
    doc = await workspace.get()
    assert doc.data["settings_yaml_seed"] == {"path": "config.yaml", "hash": "abc"}


async def test_seed_rejects_when_overrides_already_hold_rows(sessions: SessionFactory) -> None:
    seeder = SqlSettingsSeedRepository(sessions)
    overrides = SqlSettingOverrideRepository(sessions)
    await overrides.put(_override(tenant_id="other-tenant"), expected_version=None)

    with pytest.raises(HarborConflictError):
        await seeder.seed(
            tenant_id="acme",
            overrides=(_override(),),
            workspace_tenant_id="DEFAULT",
            marker_key="settings_yaml_seed",
            marker_value={"path": "config.yaml", "hash": "abc"},
        )


async def test_a_mid_seed_failure_rolls_back_every_write(sessions: SessionFactory) -> None:
    """A duplicate key in the planned overrides fails the second insert's
    primary-key constraint; the whole transaction, including the already
    `session.add`-ed first override, the revision row, and the marker, must
    roll back -- none of it may land half-committed.
    """
    seeder = SqlSettingsSeedRepository(sessions)
    overrides = SqlSettingOverrideRepository(sessions)
    revisions = SqlSettingScopeRevisionRepository(sessions)
    workspace = SqlSettingsRepository(sessions)

    with pytest.raises(sa.exc.SQLAlchemyError):
        await seeder.seed(
            tenant_id="acme",
            overrides=(_override(), _override()),  # duplicate primary key
            workspace_tenant_id="DEFAULT",
            marker_key="settings_yaml_seed",
            marker_value={"path": "config.yaml", "hash": "abc"},
        )

    assert await overrides.count_all() == 0
    assert await revisions.get("acme", "tenant", "acme") == 0
    doc = await workspace.get()
    assert "settings_yaml_seed" not in doc.data


async def test_seed_with_no_overrides_still_writes_only_the_marker(
    sessions: SessionFactory,
) -> None:
    """An empty `settings:` section must not bump a revision nobody asked for."""
    seeder = SqlSettingsSeedRepository(sessions)
    revisions = SqlSettingScopeRevisionRepository(sessions)
    workspace = SqlSettingsRepository(sessions)

    await seeder.seed(
        tenant_id="DEFAULT",
        overrides=(),
        workspace_tenant_id="DEFAULT",
        marker_key="settings_yaml_seed",
        marker_value={"path": "config.yaml", "hash": "abc"},
    )

    assert await revisions.get("DEFAULT", "tenant", "DEFAULT") == 0
    doc = await workspace.get()
    assert doc.data["settings_yaml_seed"] == {"path": "config.yaml", "hash": "abc"}
