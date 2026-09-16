"""Both public surfaces retain shared, tenant-authorized session history."""

import pytest
from app_test_conversations import auth, hmac_app, message, seed
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient


@pytest.fixture
def service():
    return MockAppService()


@pytest.fixture
def client(monkeypatch, service):
    with TestClient(hmac_app(monkeypatch, service)) as client:
        yield client


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["chat", "agent"])
async def test_session_history_paging_rename_and_erasure(client, service, surface):
    content = "  indented evidence\n    next line\n"
    await seed(
        service,
        "session-1",
        kind=surface,
        messages=(message("message-1", content=content), message("message-2")),
    )
    path = f"/v1/{surface}/sessions"
    listed = client.get(path, headers=auth())
    assert listed.headers["cache-control"] == "no-store"
    assert listed.json()["sessions"][0]["session_id"] == "session-1"
    assert "conversations" not in listed.json()
    history = client.get(f"{path}/session-1/messages", params={"limit": 1}, headers=auth())
    assert history.status_code == 200
    assert history.headers["cache-control"] == "no-store"
    assert history.json()["messages"][0]["content"] == content
    page = client.get(
        f"{path}/session-1/messages",
        params={"after": history.json()["next_cursor"]},
        headers=auth(),
    ).json()
    assert [row["message_id"] for row in page["messages"]] == ["message-2"]
    assert "next_cursor" not in page
    renamed = client.patch(f"{path}/session-1", json={"title": "New title"}, headers=auth())
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "New title"
    assert client.delete(f"{path}/session-1", headers=auth()).status_code == 200
    assert client.get(f"{path}/session-1/messages", headers=auth()).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["chat", "agent"])
async def test_session_routes_cannot_cross_tenant_boundary(client, service, surface):
    await seed(service, "private-session", kind=surface, tenant="OTHER")
    path = f"/v1/{surface}/sessions"
    assert client.get(path, params={"tenant": "OTHER"}, headers=auth()).status_code == 403
    assert client.get(path, headers=auth()).json()["sessions"] == []
    assert client.get(f"{path}/private-session/messages", headers=auth()).status_code == 404
    assert (
        client.patch(f"{path}/private-session", json={"title": "x"}, headers=auth()).status_code
        == 404
    )
    assert client.delete(f"{path}/private-session", headers=auth()).status_code == 404


@pytest.mark.parametrize("surface,wrong_mode", [("chat", "agent"), ("agent", "rag")])
@pytest.mark.parametrize("stream", [False, True])
def test_endpoint_owns_execution_mode(client, service, surface, wrong_mode, stream):
    response = client.post(
        f"/v1/{surface}/completions",
        json={"prompt": "Explain the release", "mode": wrong_mode, "stream": stream},
        headers=auth(),
    )
    assert response.status_code == 422
    assert not service.chat_calls and not service.agent_calls
    assert not getattr(service, "scope_calls", [])


@pytest.mark.parametrize("surface", ["chat", "agent"])
def test_session_list_kind_is_endpoint_owned(client, surface):
    for name in ("chat", "agent"):
        assert client.post(f"/v1/{name}/sessions", json={}, headers=auth()).status_code == 201
    response = client.get(
        f"/v1/{surface}/sessions",
        params={"kind": "agent" if surface == "chat" else "chat"},
        headers=auth(),
    )
    assert [row["kind"] for row in response.json()["sessions"]] == [surface]
