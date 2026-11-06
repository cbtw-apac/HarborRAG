"""Cursor paging and write preconditions shared by every route (Story 2 AC11, AC12)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app_test_connections import connection_world
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control.control_plane.projects import ProjectService
from harborrag_core.domain.project import Project


class _App(MockAppService):
    def __init__(self, projects: int) -> None:
        super().__init__()
        world = connection_world(
            [
                Project(id=f"p{index:02d}", name=f"P{index}", collection="c", tenant_id="DEFAULT")
                for index in range(projects)
            ]
        )
        self.connection_service = world.service
        self.project_service = ProjectService(
            control_plane=lambda: world.control_plane,
            runs=world.service._runs,
            connections=world.service,
        )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    service = _App(projects=45)
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    with TestClient(create_fastapi_app(ApiSettings())) as test_client:
        yield test_client


def test_a_cursor_walk_has_no_repeats_or_gaps(client: TestClient) -> None:
    pages: list[list[str]] = []
    cursor: str | None = None
    while True:
        params: dict[str, object] = {"limit": 20, **({"cursor": cursor} if cursor else {})}
        body = client.get("/v1/admin/projects", params=params).json()
        pages.append([item["project_id"] for item in body["items"]])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert [len(page) for page in pages] == [20, 20, 5]
    assert [project for page in pages for project in page] == [f"p{i:02d}" for i in range(45)]


def test_the_default_page_is_twenty(client: TestClient) -> None:
    assert len(client.get("/v1/admin/projects").json()["items"]) == 20


@pytest.mark.parametrize("path", ["/v1/admin/projects", "/v1/admin/connections"])
@pytest.mark.parametrize(
    ("params", "field"),
    [({"limit": 0}, "limit"), ({"limit": 101}, "limit"), ({"cursor": "garbage"}, "cursor")],
)
def test_out_of_range_paging_names_its_field(
    client: TestClient, path: str, params: dict[str, object], field: str
) -> None:
    response = client.get(path, params=params)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert [item["field"] for item in response.json()["error"]["details"]] == [field]


def test_a_garbage_chat_session_cursor_names_its_field(client: TestClient) -> None:
    response = client.get("/v1/chat/sessions", params={"cursor": "garbage"})

    assert response.status_code == 422
    assert [item["field"] for item in response.json()["error"]["details"]] == ["cursor"]


def test_an_etag_cycle_refuses_a_stale_write(client: TestClient) -> None:
    etag = client.get("/v1/admin/projects/p01").headers["etag"]
    body = {"project_name": "CPM candidates"}

    first = client.put(
        "/v1/admin/projects/p01",
        json={**body, "project_description": "v2"},
        headers={"If-Match": etag},
    )
    stale = client.put(
        "/v1/admin/projects/p01",
        json={**body, "project_description": "v3"},
        headers={"If-Match": etag},
    )

    assert first.status_code == 200
    assert first.headers["etag"] != etag
    assert stale.status_code == 412
    assert stale.json()["error"]["code"] == "precondition_failed"
    assert client.get("/v1/admin/projects/p01").json()["project_description"] == "v2"


def test_an_idempotency_key_reused_with_another_body_is_a_validation_error(
    client: TestClient,
) -> None:
    first = client.post("/v1/chat/completions", json={"prompt": "One", "idempotency_key": "key-1"})
    reused = client.post("/v1/chat/completions", json={"prompt": "Two", "idempotency_key": "key-1"})

    assert first.status_code == 200
    assert reused.status_code == 422
    assert reused.json()["error"]["code"] == "validation_error"
    assert [item["field"] for item in reused.json()["error"]["details"]] == ["Idempotency-Key"]
