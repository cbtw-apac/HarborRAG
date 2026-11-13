"""SettingOverridesConfigStore: config_store ports over setting_overrides/setting_scope_revisions."""

from __future__ import annotations

import pytest

from harborrag_adapters.repositories.database.control_plane.config_store_adapter import (
    SettingOverridesConfigStore,
)
from harborrag_core.config_store import SettingsLevel
from harborrag_core.domain.setting_override import SettingOverride
from harborrag_core.testing.control_plane_fakes import (
    FakeSettingOverrideRepository,
    FakeSettingScopeRevisionRepository,
)

pytestmark = pytest.mark.unit


def _store() -> SettingOverridesConfigStore:
    return SettingOverridesConfigStore(
        overrides=FakeSettingOverrideRepository(),
        revisions=FakeSettingScopeRevisionRepository(),
    )


def _seed_cascade(store: SettingOverridesConfigStore) -> None:
    """chunking.max_tokens: 512 at tenant, 384 at project cpm, 256 at connection cpm/wiki."""
    overrides = store.overrides.overrides
    overrides[("acme", "tenant", "acme", "chunking.max_tokens")] = SettingOverride(
        tenant_id="acme", scope_level="tenant", scope_id="acme", key="chunking.max_tokens", value=512
    )
    overrides[("acme", "project", "cpm", "chunking.max_tokens")] = SettingOverride(
        tenant_id="acme", scope_level="project", scope_id="cpm", key="chunking.max_tokens", value=384
    )
    overrides[("acme", "connection", "cpm/wiki", "chunking.max_tokens")] = SettingOverride(
        tenant_id="acme",
        scope_level="connection",
        scope_id="cpm/wiki",
        key="chunking.max_tokens",
        value=256,
    )
    store.revisions.revisions[("acme", "tenant", "acme")] = 1
    store.revisions.revisions[("acme", "project", "cpm")] = 3
    store.revisions.revisions[("acme", "connection", "cpm/wiki")] = 7


@pytest.mark.asyncio
async def test_effective_resolves_the_cascade_and_reports_the_scopes_own_revision() -> None:
    store = _store()
    _seed_cascade(store)

    connection = await store.effective(
        "acme", SettingsLevel("connection", project_id="cpm", connection_id="wiki")
    )
    assert connection.value("chunking.max_tokens") == 256
    assert connection.values["chunking.max_tokens"].origin == "connection"
    assert connection.revision == 7

    sibling = await store.effective(
        "acme", SettingsLevel("connection", project_id="cpm", connection_id="jira")
    )
    assert sibling.value("chunking.max_tokens") == 384
    assert sibling.values["chunking.max_tokens"].origin == "project"
    assert sibling.revision == 0

    project = await store.effective("acme", SettingsLevel("project", project_id="other"))
    assert project.value("chunking.max_tokens") == 512
    assert project.values["chunking.max_tokens"].origin == "tenant"

    tenant = await store.effective("acme", SettingsLevel("tenant"))
    assert tenant.override("ingestion.concurrency") is None


@pytest.mark.asyncio
async def test_effective_default_origin_when_nothing_is_set() -> None:
    store = _store()
    effective = await store.effective("acme", SettingsLevel("tenant"))
    assert effective.values["chunking.max_tokens"].origin == "default"
    assert effective.value("chunking.max_tokens") == 1100


@pytest.mark.asyncio
async def test_snapshot_groups_overrides_by_level_label() -> None:
    store = _store()
    _seed_cascade(store)
    snapshot = await store.snapshot("acme")
    assert snapshot.at(SettingsLevel("tenant")) == {"chunking.max_tokens": 512}
    assert snapshot.at(SettingsLevel("project", project_id="cpm")) == {"chunking.max_tokens": 384}


@pytest.mark.asyncio
async def test_write_is_not_supported_through_this_port() -> None:
    store = _store()
    with pytest.raises(NotImplementedError):
        await store.write(
            "acme", [], expected_revision=0, source="api", created_by="tester"
        )


@pytest.mark.asyncio
async def test_ping_issues_a_real_query() -> None:
    store = _store()
    await store.ping()
