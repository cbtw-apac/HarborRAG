"""Control-plane write use cases (ML2): create/update/delete_source semantics."""

from __future__ import annotations

import pytest

from harborrag_app.workflow_control import mock_app_service
from harborrag_core.contracts.errors import (
    HarborCapabilityError,
    HarborNotFoundError,
    HarborValidationError,
)
from harborrag_core.domain.project import Project

JIRA_CONFIG = {
    "base_url": "https://example.atlassian.net",
    "email": "person@example.com",
    "token": "hunter2",
}


@pytest.mark.asyncio
async def test_create_source_extracts_secret_fields_and_logs_activity() -> None:
    service = mock_app_service(projects=[Project(id="proj-a", name="A", collection="a")])
    response = await service.create_source(
        project_id="proj-a",
        source_type="jira",
        name="Support board",
        config=dict(JIRA_CONFIG),
        schedule=None,
        actor="alice@example.com",
    )
    assert response.ok
    source = response.data["source"]
    assert source.config["base_url"] == "https://example.atlassian.net"
    assert set(source.config["token"]) == {"secret_ref"}
    assert source.secret_refs == [source.config["token"]["secret_ref"]]

    control_plane = service._control_plane()
    assert await control_plane.secrets.resolve(source.secret_refs[0]) == "hunter2"
    [entry] = await control_plane.activity.list()
    assert entry.actor == "alice@example.com"
    assert entry.verb == "created"
    assert entry.entity_type == "source"
    assert "hunter2" not in entry.summary


@pytest.mark.asyncio
async def test_create_source_rejects_unknown_project() -> None:
    service = mock_app_service()
    with pytest.raises(HarborNotFoundError):
        await service.create_source(
            project_id="ghost",
            source_type="jira",
            name="x",
            config=dict(JIRA_CONFIG),
            schedule=None,
            actor="alice@example.com",
        )


@pytest.mark.asyncio
async def test_create_source_rejects_unsupported_source_type() -> None:
    service = mock_app_service(projects=[Project(id="proj-a", name="A", collection="a")])
    with pytest.raises(HarborCapabilityError):
        await service.create_source(
            project_id="proj-a",
            source_type="notion",
            name="x",
            config={},
            schedule=None,
            actor="alice@example.com",
        )


@pytest.mark.asyncio
async def test_create_source_rejects_unknown_config_field() -> None:
    service = mock_app_service(projects=[Project(id="proj-a", name="A", collection="a")])
    with pytest.raises(HarborValidationError):
        await service.create_source(
            project_id="proj-a",
            source_type="jira",
            name="x",
            config={**JIRA_CONFIG, "not_a_real_field": "x"},
            schedule=None,
            actor="alice@example.com",
        )


@pytest.mark.asyncio
async def test_update_source_omitting_config_leaves_secret_untouched() -> None:
    service = mock_app_service(projects=[Project(id="proj-a", name="A", collection="a")])
    created = (
        await service.create_source(
            project_id="proj-a",
            source_type="jira",
            name="Support board",
            config=dict(JIRA_CONFIG),
            schedule=None,
            actor="alice@example.com",
        )
    ).data["source"]
    original_ref = created.secret_refs[0]

    updated = (
        await service.update_source(
            created.id,
            updates={"name": "Renamed board"},
            actor="bob@example.com",
        )
    ).data["source"]
    assert updated.name == "Renamed board"
    assert updated.secret_refs == [original_ref]
    control_plane = service._control_plane()
    assert await control_plane.secrets.resolve(original_ref) == "hunter2"


@pytest.mark.asyncio
async def test_update_source_rotating_secret_value_retires_old_ref() -> None:
    service = mock_app_service(projects=[Project(id="proj-a", name="A", collection="a")])
    created = (
        await service.create_source(
            project_id="proj-a",
            source_type="jira",
            name="Support board",
            config=dict(JIRA_CONFIG),
            schedule=None,
            actor="alice@example.com",
        )
    ).data["source"]
    original_ref = created.secret_refs[0]

    updated = (
        await service.update_source(
            created.id,
            updates={"config": {"token": "new-token-value"}},
            actor="bob@example.com",
        )
    ).data["source"]
    new_ref = updated.secret_refs[0]
    assert new_ref != original_ref
    # base_url survives even though this update only mentioned "token".
    assert updated.config["base_url"] == "https://example.atlassian.net"

    control_plane = service._control_plane()
    assert await control_plane.secrets.resolve(new_ref) == "new-token-value"
    with pytest.raises(KeyError):
        await control_plane.secrets.resolve(original_ref)


@pytest.mark.asyncio
async def test_update_source_unknown_id_raises_not_found() -> None:
    service = mock_app_service()
    with pytest.raises(HarborNotFoundError):
        await service.update_source("ghost", updates={"name": "x"}, actor="alice@example.com")


@pytest.mark.asyncio
async def test_update_source_rejects_unsupported_field() -> None:
    service = mock_app_service(projects=[Project(id="proj-a", name="A", collection="a")])
    created = (
        await service.create_source(
            project_id="proj-a",
            source_type="jira",
            name="Support board",
            config=dict(JIRA_CONFIG),
            schedule=None,
            actor="alice@example.com",
        )
    ).data["source"]
    with pytest.raises(HarborValidationError):
        await service.update_source(
            created.id, updates={"project_id": "elsewhere"}, actor="alice@example.com"
        )


@pytest.mark.asyncio
async def test_delete_source_forgets_secrets_and_logs_activity() -> None:
    service = mock_app_service(projects=[Project(id="proj-a", name="A", collection="a")])
    created = (
        await service.create_source(
            project_id="proj-a",
            source_type="jira",
            name="Support board",
            config=dict(JIRA_CONFIG),
            schedule=None,
            actor="alice@example.com",
        )
    ).data["source"]
    ref = created.secret_refs[0]

    response = await service.delete_source(created.id, actor="alice@example.com")
    assert response.ok
    control_plane = service._control_plane()
    assert await control_plane.sources.get(created.id) is None
    with pytest.raises(KeyError):
        await control_plane.secrets.resolve(ref)
    verbs = [entry.verb for entry in await control_plane.activity.list()]
    assert verbs == ["deleted", "created"]


@pytest.mark.asyncio
async def test_delete_source_unknown_id_raises_not_found() -> None:
    service = mock_app_service()
    with pytest.raises(HarborNotFoundError):
        await service.delete_source("ghost", actor="alice@example.com")
