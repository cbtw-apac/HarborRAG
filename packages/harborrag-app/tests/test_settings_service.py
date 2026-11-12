"""SettingsService decides the role a level needs and refuses a malformed PATCH, over a migrated SQLite store."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from app_test_connections import FakeSecrets, fake_control_plane
from sqlalchemy.ext.asyncio import async_sessionmaker

from harborrag_adapters.repositories.database.control_plane.config_store import SqlSettingsStore
from harborrag_adapters.repositories.database.control_plane.engine import (
    create_control_plane_engine,
)
from harborrag_adapters.repositories.database.control_plane.migrations import run_migrations
from harborrag_app.operations.errors import error_body
from harborrag_core.config_store import SettingsRegistry
from harborrag_core.contracts.errors import HarborAuthError, HarborValidationError
from harborrag_core.domain.project import Project
from harborrag_engine.control_plane.connection_views import ANY_VERSION
from harborrag_runtime.application.control_plane.settings_service import (
    SettingsPatch,
    SettingsService,
)
from harborrag_runtime.application.errors import SettingsLevelNotFoundError
from harborrag_runtime.composition import ControlPlaneRepositories
from harborrag_runtime.config_store import CachedConfigProvider

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_TENANT = "acme"
_KEY = "chunking.max_tokens"


@pytest.fixture
def service(tmp_path: Path) -> SettingsService:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'control.db'}"
    run_migrations(dsn)
    store = SqlSettingsStore(
        async_sessionmaker(create_control_plane_engine(dsn), expire_on_commit=False)
    )
    registry = SettingsRegistry.default()
    plane: ControlPlaneRepositories = replace(
        fake_control_plane(
            [Project(id="engineering", name="Engineering", collection="eng", tenant_id=_TENANT)],
            FakeSecrets(),
        ),
        config_store=store,
        config=CachedConfigProvider(store, registry),
    )
    return SettingsService(control_plane=lambda: plane, registry=registry)


def _patch(
    *,
    level: str | None = "tenant",
    expected_revision: int | None = 0,
    reindex: bool = False,
) -> SettingsPatch:
    return SettingsPatch(
        level=level,
        body={_KEY: 300},
        expected_revision=expected_revision,
        dry_run=False,
        reindex=reindex,
    )


@pytest.mark.parametrize("level", [None, "tenant", "project:engineering"])
async def test_an_editor_may_not_read_a_level_that_is_not_a_connections(
    service: SettingsService, level: str | None
) -> None:
    with pytest.raises(HarborAuthError) as refused:
        await service.read(tenant_id=_TENANT, level=level, effective=False, role="editor")

    body = error_body(refused.value)
    assert (body.status, body.message) == (403, "requires admin role")


async def test_an_editor_may_read_a_connections_level_past_the_role_check(
    service: SettingsService,
) -> None:
    with pytest.raises(SettingsLevelNotFoundError):
        await service.read(
            tenant_id=_TENANT,
            level="connection:engineering/jira-main",
            effective=False,
            role="editor",
        )


async def test_a_malformed_level_is_a_validation_error_before_any_role_refusal(
    service: SettingsService,
) -> None:
    with pytest.raises(HarborValidationError) as refused:
        await service.read(tenant_id=_TENANT, level="team:x", effective=False, role="editor")

    assert refused.value.details["field"] == "level"


async def test_an_admin_reads_the_tenants_overrides_at_revision_zero(
    service: SettingsService,
) -> None:
    read = await service.read(tenant_id=_TENANT, level=None, effective=False, role="admin")

    assert (read.level, read.revision, read.values) == ("tenant", 0, {})


async def test_a_patch_without_a_revision_is_refused_on_if_match(service: SettingsService) -> None:
    with pytest.raises(HarborValidationError) as refused:
        await service.patch(
            tenant_id=_TENANT, request=_patch(expected_revision=None), actor="u_demo"
        )

    assert error_body(refused.value).details == [
        {"field": "If-Match", "issue": "If-Match is required"}
    ]


async def test_a_patch_that_asks_for_a_reindex_is_refused_before_the_missing_revision(
    service: SettingsService,
) -> None:
    with pytest.raises(HarborValidationError) as refused:
        await service.patch(
            tenant_id=_TENANT,
            request=_patch(expected_revision=None, reindex=True),
            actor="u_demo",
        )

    assert refused.value.details["field"] == "reindex"


async def test_a_patch_of_a_malformed_level_is_refused_first(service: SettingsService) -> None:
    with pytest.raises(HarborValidationError) as refused:
        await service.patch(
            tenant_id=_TENANT,
            request=_patch(level="team:x", expected_revision=None, reindex=True),
            actor="u_demo",
        )

    assert refused.value.details["field"] == "level"


async def test_a_patch_saves_the_next_revision(service: SettingsService) -> None:
    patched = await service.patch(
        tenant_id=_TENANT, request=_patch(expected_revision=ANY_VERSION), actor="u_demo"
    )

    assert (patched.revision, [change.key for change in patched.changed]) == (1, [_KEY])
