from __future__ import annotations

from dataclasses import dataclass

import pytest

from harborrag_app.api.errors import ReferenceNotFoundError, ReindexRequiredError
from harborrag_app.workflow_control.control_plane.settings import (
    SettingsChange,
    SettingsService,
    parse_level,
)
from harborrag_core.contracts.errors import HarborPreconditionFailedError, HarborValidationError
from harborrag_core.domain.connection import Connection
from harborrag_core.domain.project import Project
from harborrag_core.domain.setting_override import SettingOverride
from harborrag_core.testing.control_plane_fakes import (
    FakeConnectionRepository,
    FakeProjectRepository,
    FakeSettingOverrideRepository,
    FakeSettingScopeRevisionRepository,
)


@dataclass(slots=True)
class _CP:
    """Just the ports SettingsService touches; lighter than a full ControlPlaneRepositories."""

    setting_overrides: FakeSettingOverrideRepository
    setting_scope_revisions: FakeSettingScopeRevisionRepository
    projects: FakeProjectRepository
    connections: FakeConnectionRepository


def _project(project_id: str, tenant_id: str = "acme") -> Project:
    return Project(id=project_id, name=project_id, collection=project_id, tenant_id=tenant_id)


def _connection(project_id: str, connection_id: str, tenant_id: str = "acme") -> Connection:
    return Connection(
        id=f"{tenant_id}-{project_id}-{connection_id}",
        tenant_id=tenant_id,
        project_id=project_id,
        connection_id=connection_id,
        source_key=f"{project_id}/{connection_id}",
        source_type="jira",
        name=connection_id,
    )


def _service() -> tuple[SettingsService, _CP]:
    cp = _CP(
        FakeSettingOverrideRepository(),
        FakeSettingScopeRevisionRepository(),
        FakeProjectRepository(
            {
                "cpm": _project("cpm"),
                "other": _project("other"),
            }
        ),
        FakeConnectionRepository(
            {
                "cpm-tickets": _connection("cpm", "cpm-tickets"),
                "cpm-docs": _connection("cpm", "cpm-docs"),
            }
        ),
    )
    return SettingsService(control_plane=lambda: cp), cp


class TestParseLevel:
    def test_tenant(self) -> None:
        assert parse_level("tenant") == ("tenant", "tenant")

    def test_project(self) -> None:
        assert parse_level("project:cpm") == ("project", "cpm")

    def test_connection(self) -> None:
        assert parse_level("connection:cpm/cpm-tickets") == ("connection", "cpm/cpm-tickets")

    def test_rejects_garbage(self) -> None:
        with pytest.raises(HarborValidationError):
            parse_level("workspace:whatever")

    def test_rejects_invalid_project_id(self) -> None:
        with pytest.raises(HarborValidationError):
            parse_level("project:Has Spaces")


@pytest.mark.asyncio
async def test_read_raw_only_shows_overrides_stored_at_exactly_this_scope() -> None:
    service, cp = _service()
    await cp.setting_overrides.put(
        _override("acme", "tenant", "acme", "chunking.max_tokens", 512), expected_version=None
    )
    await cp.setting_overrides.put(
        _override("acme", "project", "cpm", "chunking.max_tokens", 384), expected_version=None
    )

    result = await service.read("acme", "project", "cpm", effective=False)

    assert result.values == {"chunking.max_tokens": 384}
    assert result.level == "project:cpm"


@pytest.mark.asyncio
async def test_read_effective_cascades_default_tenant_project_connection() -> None:
    service, cp = _service()
    await cp.setting_overrides.put(
        _override("acme", "tenant", "acme", "chunking.max_tokens", 512), expected_version=None
    )
    await cp.setting_overrides.put(
        _override("acme", "project", "cpm", "chunking.max_tokens", 384), expected_version=None
    )
    await cp.setting_overrides.put(
        _override("acme", "connection", "cpm/cpm-tickets", "chunking.max_tokens", 256),
        expected_version=None,
    )

    at_connection = await service.read("acme", "connection", "cpm/cpm-tickets", effective=True)
    assert at_connection.values["chunking.max_tokens"] == {"value": 256, "origin": "connection"}

    at_sibling_connection = await service.read("acme", "connection", "cpm/cpm-docs", effective=True)
    assert at_sibling_connection.values["chunking.max_tokens"] == {
        "value": 384,
        "origin": "project",
    }

    at_other_project = await service.read("acme", "project", "other", effective=True)
    assert at_other_project.values["chunking.max_tokens"] == {"value": 512, "origin": "tenant"}

    at_other_tenant = await service.read("other", "tenant", "other", effective=True)
    assert at_other_tenant.values["chunking.max_tokens"] == {"value": 1100, "origin": "default"}


@pytest.mark.asyncio
async def test_patch_rejects_unknown_key() -> None:
    service, _cp = _service()
    with pytest.raises(ReferenceNotFoundError):
        await service.patch(
            "acme",
            "tenant",
            "acme",
            {"not.a.real.key": 1},
            expected_revision=0,
            actor="tester",
            dry_run=False,
        )


@pytest.mark.asyncio
async def test_patch_rejects_key_not_allowed_at_this_level() -> None:
    service, _cp = _service()
    with pytest.raises(HarborValidationError):
        await service.patch(
            "acme",
            "project",
            "cpm",
            {"ingestion.concurrency": 8},
            expected_revision=0,
            actor="tester",
            dry_run=False,
        )


@pytest.mark.asyncio
async def test_patch_rejects_embedding_model_change_with_reindex_required() -> None:
    service, _cp = _service()
    with pytest.raises(ReindexRequiredError):
        await service.patch(
            "acme",
            "tenant",
            "acme",
            {"models.embedding": "openai:text-embedding-3-large"},
            expected_revision=0,
            actor="tester",
            dry_run=False,
        )


@pytest.mark.asyncio
async def test_patch_allows_setting_embedding_model_to_its_own_current_value() -> None:
    """Re-asserting the same value is not a change, so it does not need reindex=true."""
    service, cp = _service()
    await cp.setting_overrides.put(
        _override("acme", "tenant", "acme", "models.embedding", "openai:text-embedding-3-large"),
        expected_version=None,
    )
    result = await service.patch(
        "acme",
        "tenant",
        "acme",
        {"models.embedding": "openai:text-embedding-3-large"},
        expected_revision=0,
        actor="tester",
        dry_run=False,
    )
    assert result.changed[0].before == result.changed[0].after


@pytest.mark.asyncio
async def test_patch_rejects_a_stale_revision() -> None:
    service, _cp = _service()
    with pytest.raises(HarborPreconditionFailedError):
        await service.patch(
            "acme",
            "tenant",
            "acme",
            {"chunking.max_tokens": 384},
            expected_revision=5,
            actor="tester",
            dry_run=False,
        )


@pytest.mark.asyncio
async def test_patch_writes_and_bumps_revision() -> None:
    service, cp = _service()
    result = await service.patch(
        "acme",
        "tenant",
        "acme",
        {"chunking.max_tokens": 384},
        expected_revision=0,
        actor="tester",
        dry_run=False,
    )
    assert result.revision == 1
    assert result.changed == [
        _change("chunking.max_tokens", 1100, 384, "next_ingestion"),
    ]
    assert await cp.setting_overrides.get_at("acme", "tenant", "acme") == {
        "chunking.max_tokens": 384
    }


@pytest.mark.asyncio
async def test_patch_null_value_removes_the_override() -> None:
    service, cp = _service()
    await cp.setting_overrides.put(
        _override("acme", "tenant", "acme", "chunking.max_tokens", 384), expected_version=None
    )
    result = await service.patch(
        "acme",
        "tenant",
        "acme",
        {"chunking.max_tokens": None},
        expected_revision=0,
        actor="tester",
        dry_run=False,
    )
    assert result.changed[0].after == 1100  # falls back to the registry default
    assert await cp.setting_overrides.get_at("acme", "tenant", "acme") == {}


@pytest.mark.asyncio
async def test_dry_run_previews_without_writing_or_bumping() -> None:
    service, cp = _service()
    result = await service.patch(
        "acme",
        "tenant",
        "acme",
        {"chunking.max_tokens": 384},
        expected_revision=0,
        actor="tester",
        dry_run=True,
    )
    assert result.dry_run is True
    assert result.revision == 0
    assert await cp.setting_overrides.count_all() == 0
    assert await cp.setting_scope_revisions.get("acme", "tenant", "acme") == 0


@pytest.mark.asyncio
async def test_read_a_nonexistent_project_is_reference_not_found() -> None:
    service, _cp = _service()
    with pytest.raises(ReferenceNotFoundError):
        await service.read("acme", "project", "nope", effective=False)


@pytest.mark.asyncio
async def test_read_a_nonexistent_connection_is_reference_not_found() -> None:
    service, _cp = _service()
    with pytest.raises(ReferenceNotFoundError):
        await service.read("acme", "connection", "cpm/bogus", effective=False)


@pytest.mark.asyncio
async def test_patch_a_nonexistent_project_is_reference_not_found() -> None:
    service, _cp = _service()
    with pytest.raises(ReferenceNotFoundError):
        await service.patch(
            "acme",
            "project",
            "nope",
            {"chunking.max_tokens": 384},
            expected_revision=0,
            actor="tester",
            dry_run=False,
        )


def _override(
    tenant_id: str, scope_level: str, scope_id: str, key: str, value: object
) -> SettingOverride:
    return SettingOverride(
        tenant_id=tenant_id,
        scope_level=scope_level,  # type: ignore[arg-type]
        scope_id=scope_id,
        key=key,
        value=value,
    )


def _change(key: str, before: object, after: object, applies: str) -> SettingsChange:
    return SettingsChange(key=key, before=before, after=after, applies=applies)  # type: ignore[arg-type]
