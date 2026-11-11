"""LocalOperations runs the admin operations over the connection and project services."""

from __future__ import annotations

from typing import Any

import pytest
from app_test_connections import connection_world
from app_test_fixtures import MockAppService

from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.v1.admin.connection_schemas import (
    ConnectionWriteRequest,
    ProjectPage,
    ProjectWriteRequest,
)
from harborrag_app.operations import (
    ConnectionRef,
    LocalOperations,
    Saved,
    Scope,
    Tagged,
    TenantForbiddenError,
)
from harborrag_app.workflow_control.control_plane.projects import ProjectService
from harborrag_app.workflow_control.control_plane.settings_service import SettingsRead
from harborrag_core.config_store import SettingsLevel
from harborrag_core.contracts.errors import (
    HarborAuthError,
    HarborPreconditionFailedError,
    HarborValidationError,
)
from harborrag_core.domain.project import Project

pytestmark = pytest.mark.unit

_REF = ConnectionRef("engineering", "jira-main")
_BODY = ConnectionWriteRequest(
    source_type="jira",
    name="Engineering Jira",
    credentials={
        "base_url": "https://example.atlassian.net",
        "email": "bot@example.com",
        "api_token": "demo-token-sentinel-0001",
    },
    config={"jql": "project = DEMO"},
)
_DEMO = ProjectWriteRequest(project_name="DEMO", project_description="Demo tickets")


class _AdminApp(MockAppService):
    """Mock app service carrying real connection and project services over fakes."""

    def __init__(self) -> None:
        super().__init__()
        world = connection_world(
            [Project(id="engineering", name="Engineering", collection="eng", tenant_id="acme")]
        )
        self.connection_service = world.service
        self.project_service = ProjectService(
            control_plane=lambda: world.control_plane,
            runs=world.service._runs,
            connections=world.service,
        )


def _scope(role: Any = "editor", tenant_id: str = "acme") -> Scope:
    return Scope(Principal("u_demo", role, frozenset({"acme"})), tenant_id)


@pytest.fixture
def operations() -> LocalOperations:
    return LocalOperations(_AdminApp())


@pytest.mark.asyncio
async def test_put_connection_creates_once_and_tags_each_revision(
    operations: LocalOperations,
) -> None:
    created = await operations.put_connection(
        _scope(), _REF, _BODY, if_match=None, if_none_match="*"
    )
    with pytest.raises(HarborPreconditionFailedError):
        await operations.put_connection(_scope(), _REF, _BODY, if_match=None, if_none_match="*")
    with pytest.raises(HarborPreconditionFailedError):
        await operations.put_connection(
            _scope(), _REF, _BODY, if_match='W/"rev-9"', if_none_match=None
        )
    read = await operations.get_connection(_scope(), _REF)

    assert isinstance(created, Saved)
    assert (created.created, created.etag) == (True, 'W/"rev-1"')
    assert created.body.connection_ref == "engineering/jira-main"
    assert created.body.created_by == "u_demo"
    assert isinstance(read, Tagged)
    assert read.etag == created.etag
    assert read.body.connection_ref == "engineering/jira-main"
    assert "sentinel" not in read.body.model_dump_json()


@pytest.mark.asyncio
async def test_a_reader_may_not_list_connections(operations: LocalOperations) -> None:
    with pytest.raises(HarborAuthError) as refused:
        await operations.list_connections(
            _scope("reader"), project_id=None, source_type=None, status=None, limit=20, cursor=None
        )

    assert type(refused.value) is HarborAuthError
    assert refused.value.forbidden is True


@pytest.mark.asyncio
async def test_a_tenant_the_credential_does_not_hold_is_refused(
    operations: LocalOperations,
) -> None:
    with pytest.raises(TenantForbiddenError):
        await operations.list_connections(
            _scope("owner", tenant_id="DEFAULT"),
            project_id=None,
            source_type=None,
            status=None,
            limit=20,
            cursor=None,
        )


@pytest.mark.asyncio
async def test_put_project_maps_the_request_names_onto_the_service(
    operations: LocalOperations,
) -> None:
    saved = await operations.put_project(
        _scope("admin"), "demo", _DEMO, if_match=None, if_none_match=None
    )
    read = await operations.get_project(_scope("admin"), "demo")

    assert (saved.created, saved.etag) == (True, 'W/"rev-1"')
    assert (read.body.project_name, read.body.project_description) == ("DEMO", "Demo tickets")
    assert read.etag == saved.etag


@pytest.mark.asyncio
async def test_list_projects_wraps_each_service_page(operations: LocalOperations) -> None:
    await operations.put_project(_scope("admin"), "demo", _DEMO, if_match=None, if_none_match=None)

    first = await operations.list_projects(_scope("admin"), q=None, limit=1, cursor=None)
    rest = await operations.list_projects(
        _scope("admin"), q=None, limit=1, cursor=first.next_cursor
    )

    assert isinstance(first, ProjectPage)
    assert [project.project_id for project in first.items] == ["demo"]
    assert first.next_cursor is not None
    assert [project.project_id for project in rest.items] == ["engineering"]
    assert rest.next_cursor is None


@pytest.mark.asyncio
async def test_an_unknown_source_type_category_is_a_validation_error(
    operations: LocalOperations,
) -> None:
    with pytest.raises(HarborValidationError) as caught:
        await operations.list_source_types(_scope(), category="chat")

    assert caught.value.details == {"field": "category"}


@pytest.mark.asyncio
async def test_deleting_an_empty_project_starts_no_job(operations: LocalOperations) -> None:
    await operations.put_project(_scope("admin"), "demo", _DEMO, if_match=None, if_none_match=None)

    assert await operations.delete_project(_scope("admin"), "demo", cascade=False) is None


class _Revisions:
    """Settings service double answering a fixed tenant revision per tenant."""

    def __init__(self, revisions: dict[str, int]) -> None:
        self.revisions = revisions

    async def read(self, *, tenant_id: str, level: SettingsLevel, effective: bool) -> SettingsRead:
        return SettingsRead(str(level), self.revisions.get(tenant_id, 0), {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tenant_id", "expected"), [("acme", True), ("tuned", True), ("new", False)]
)
async def test_tenant_has_data_reads_projects_connections_and_settings(
    tenant_id: str, expected: bool
) -> None:
    app = _AdminApp()
    app.settings_service = _Revisions({"tuned": 2})

    assert await LocalOperations(app).tenant_has_data(tenant_id) is expected


@pytest.mark.asyncio
async def test_tenants_with_data_lists_each_tenant_once_in_code_point_order() -> None:
    from harborrag_app.workflow_control.control_plane.tenants import tenants_with_data

    world = connection_world(
        [
            Project(id="a", name="A", collection="a", tenant_id="acme"),
            Project(id="b", name="B", collection="b", tenant_id="acme"),
            Project(id="c", name="C", collection="c", tenant_id="DEFAULT"),
        ]
    )

    assert await tenants_with_data(world.control_plane) == ["DEFAULT", "acme"]
