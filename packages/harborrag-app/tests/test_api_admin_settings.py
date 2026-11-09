"""``GET/PATCH /v1/admin/settings`` and ``GET /v1/admin/settings/schema`` over a migrated SQLite store."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from app_test_connections import ConnectionWorld, connection_world
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_api_admin_connections import _BODY, _SECRET, _token

from harborrag_adapters.repositories.database.control_plane.config_store import SqlSettingsStore
from harborrag_adapters.repositories.database.control_plane.engine import (
    create_control_plane_engine,
)
from harborrag_adapters.repositories.database.control_plane.migrations import run_migrations
from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control.control_plane.settings_service import SettingsService
from harborrag_core.config_store import SettingChange, SettingsLevel, SettingsRegistry
from harborrag_core.domain.project import Project
from harborrag_runtime.composition import ControlPlaneRepositories
from harborrag_runtime.config_store import CachedConfigProvider

_URL = "/v1/admin/settings"
_SECRET_VALUE = "s3cr3t-token-value"
_TICKETS = "connection:cpm/cpm-tickets"
_RAISED_BODY = {**_BODY, "credentials": {**_BODY["credentials"], "api_token": _SECRET_VALUE}}
_ETAG = re.compile(r'^W/"rev-[0-9]+"$')


@dataclass
class _Env:
    client: TestClient
    store: SqlSettingsStore
    plane: ControlPlaneRepositories

    def headers(self, role: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {_token(role, ['acme'])}", "X-Tenant-Id": "acme"}

    def get(self, role: str = "admin", **params: Any) -> Any:
        return self.client.get(_URL, params=params, headers=self.headers(role))

    def patch(
        self,
        body: dict[str, Any],
        *,
        if_match: str | None = 'W/"rev-3"',
        role: str = "admin",
        **params: Any,
    ) -> Any:
        headers = self.headers(role)
        if if_match is not None:
            headers["If-Match"] = if_match
        return self.client.patch(_URL, json=body, params=params, headers=headers)


class _App(MockAppService):
    def __init__(self, world: ConnectionWorld, service: SettingsService) -> None:
        super().__init__()
        self.connection_service = world.service
        self.settings_service = service


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[_Env]:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'control.db'}"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    store = SqlSettingsStore(async_sessionmaker(engine, expire_on_commit=False))
    registry = SettingsRegistry.default()
    world = connection_world(
        [
            Project(id="cpm", name="CPM", collection="cpm", tenant_id="acme"),
            Project(id="hr", name="HR", collection="hr", tenant_id="acme"),
        ]
    )
    plane = replace(
        world.control_plane, config_store=store, config=CachedConfigProvider(store, registry)
    )
    service = SettingsService(control_plane=lambda: plane, registry=registry)
    app_service = _App(world, service)
    monkeypatch.setattr(api_app, "select_app_service", lambda: (app_service, "test"))
    app = create_fastapi_app(ApiSettings(auth_mode="hmac", auth_secret=_SECRET))
    with TestClient(app) as client:
        admin = {"Authorization": f"Bearer {_token('admin', ['acme'])}", "X-Tenant-Id": "acme"}
        for path, body in (
            ("cpm/connections/cpm-tickets", _RAISED_BODY),
            ("cpm/connections/cpm-wiki", _BODY),
            ("hr/connections/hr-cvs", _BODY),
        ):
            assert (
                client.put(f"/v1/admin/projects/{path}", json=body, headers=admin).status_code
                == 201
            )
        _seed(client, store)
        yield _Env(client, store, plane)


def _seed(client: TestClient, store: SqlSettingsStore) -> None:
    portal = client.portal
    assert portal is not None
    for revision, (level, value) in enumerate(
        (("tenant", 512), ("project:cpm", 384), (_TICKETS, 256))
    ):
        change = SettingChange(level, "chunking.max_tokens", 1200, value, "next_ingestion")
        portal.call(
            lambda change=change, revision=revision: store.write(
                "acme", [change], expected_revision=revision, source="seed", created_by="system"
            )
        )


def _field(response: Any) -> str:
    return response.json()["error"]["details"][0]["field"]


def test_read_overrides_at_level(env: _Env) -> None:
    response = env.get(level="project:cpm", effective="false")

    assert response.status_code == 200
    assert response.json() == {
        "level": "project:cpm",
        "revision": "rev_3",
        "values": {"chunking.max_tokens": 384},
    }
    assert _ETAG.match(response.headers["ETag"])
    assert response.headers["ETag"] == 'W/"rev-3"'
    assert env.get().json()["level"] == "tenant"


def test_effective_view_and_precedence(env: _Env) -> None:
    def values(level: str) -> dict[str, Any]:
        return env.get(level=level, effective="true").json()["values"]

    tickets = values(_TICKETS)
    assert tickets["chunking.max_tokens"] == {"value": 256, "origin": "connection"}
    assert tickets["retrieval.top_k"] == {"value": 5, "origin": "default"}
    assert values("connection:cpm/cpm-wiki")["chunking.max_tokens"] == {
        "value": 384,
        "origin": "project",
    }
    assert values("connection:hr/hr-cvs")["chunking.max_tokens"] == {
        "value": 512,
        "origin": "tenant",
    }


def test_roles_and_no_secret_in_responses(env: _Env) -> None:
    responses = [env.get("editor", level=_TICKETS)]
    forbidden = [
        env.get("editor"),
        env.get("editor", level="project:cpm"),
        env.get("reader", level=_TICKETS),
        env.get("reader"),
    ]

    assert responses[0].status_code == 200
    for response in forbidden:
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden"
    for response in (*responses, *forbidden):
        assert _SECRET_VALUE not in response.text


def test_patch_applies_and_records_actor(env: _Env) -> None:
    response = env.patch({"chunking.max_tokens": 300}, level="project:cpm")

    assert response.status_code == 200
    assert response.json() == {
        "revision": "rev_4",
        "dry_run": False,
        "changed": [
            {"key": "chunking.max_tokens", "from": 384, "to": 300, "applies": "next_ingestion"}
        ],
    }
    assert response.headers["ETag"] == 'W/"rev-4"'
    reread = env.get(level="project:cpm")
    assert reread.json()["values"] == {"chunking.max_tokens": 300}
    assert reread.headers["ETag"] == 'W/"rev-4"'
    logged = env.client.portal.call(lambda: env.store.revision("acme", 4))  # type: ignore[union-attr]
    assert logged is not None
    assert (logged.source, logged.created_by) == ("api", "u_huy")
    assert (
        env.patch({"chunking.max_tokens": 200}, role="editor", level="project:cpm").status_code
        == 403
    )


def test_dry_run_changes_nothing(env: _Env) -> None:
    response = env.patch({"chunking.max_tokens": 300}, level="project:cpm", dry_run="true")

    assert response.status_code == 200
    assert response.json()["dry_run"] is True
    assert response.json()["changed"]
    reread = env.get(level="project:cpm")
    assert reread.json()["values"] == {"chunking.max_tokens": 384}
    assert reread.headers["ETag"] == 'W/"rev-3"'


def test_null_removes_override(env: _Env) -> None:
    response = env.patch({"chunking.max_tokens": None}, level="project:cpm")

    assert response.status_code == 200
    assert response.json()["changed"][0]["to"] is None
    effective = env.get(level="project:cpm", effective="true").json()["values"]
    assert effective["chunking.max_tokens"] == {"value": 512, "origin": "tenant"}


@pytest.mark.parametrize(
    ("body", "level", "if_match", "field"),
    [
        ({"chunking.max_tokenz": 1}, "project:cpm", 'W/"rev-3"', "chunking.max_tokenz"),
        ({"chunking.max_tokens": 99999}, "project:cpm", 'W/"rev-3"', "chunking.max_tokens"),
        ({"retrieval.top_k": 5}, _TICKETS, 'W/"rev-3"', "retrieval.top_k"),
        ({"chunking.max_tokens": 300}, "project:cpm", None, "If-Match"),
        ({"database.url": "postgres://x"}, "tenant", 'W/"rev-3"', "database.url"),
        ({"retrieval.rerank": True}, "tenant", 'W/"rev-3"', "retrieval.rerank"),
    ],
)
def test_validation_saves_nothing(
    env: _Env, body: dict[str, Any], level: str, if_match: str | None, field: str
) -> None:
    response = env.patch(body, level=level, if_match=if_match)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert _field(response) == field
    assert env.client.portal.call(lambda: env.store.latest_revision("acme")) == 3  # type: ignore[union-attr]


def test_stale_if_match_is_precondition_failed(env: _Env) -> None:
    for if_match in ('W/"rev-2"', "garbage"):
        response = env.patch({"chunking.max_tokens": 300}, level="project:cpm", if_match=if_match)

        assert response.status_code == 412
        assert response.json()["error"]["code"] == "precondition_failed"
    assert env.client.portal.call(lambda: env.store.latest_revision("acme")) == 3  # type: ignore[union-attr]


def test_reindex_refused(env: _Env) -> None:
    key = "models.embedding"
    refused = env.patch({key: "prov_openai:text-embedding-3-small"})
    flagged = env.patch({"chunking.max_tokens": 300}, level="project:cpm", reindex="true")

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "reindex_required"
    assert flagged.status_code == 422
    assert _field(flagged) == "reindex"
    assert env.client.portal.call(lambda: env.store.latest_revision("acme")) == 3  # type: ignore[union-attr]


def test_level_references(env: _Env) -> None:
    missing = env.get(level="project:nope")
    missing_patch = env.patch({"chunking.max_tokens": 300}, level="connection:cpm/nope")
    malformed = env.get(level="folder:x")

    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "reference_not_found"
    assert missing.json()["error"]["details"] == {"field": "level"}
    assert missing_patch.status_code == 422
    assert missing_patch.json()["error"]["code"] == "reference_not_found"
    assert malformed.status_code == 422
    assert malformed.json()["error"]["code"] == "validation_error"
    assert _field(malformed) == "level"


def test_if_match_star_uses_current_revision(env: _Env) -> None:
    response = env.patch({"chunking.max_tokens": 300}, level="project:cpm", if_match="*")

    assert response.status_code == 200
    assert response.json()["revision"] == "rev_4"


def test_patch_invalidates_cache(env: _Env) -> None:
    config = env.plane.config
    assert config is not None
    portal = env.client.portal
    assert portal is not None
    level = SettingsLevel.parse("project:cpm")
    assert portal.call(lambda: config.effective("acme", level)).value("chunking.max_tokens") == 384

    assert env.patch({"chunking.max_tokens": 300}, level="project:cpm").status_code == 200

    assert portal.call(lambda: config.effective("acme", level)).value("chunking.max_tokens") == 300


def test_schema_lists_registry_by_group(env: _Env) -> None:
    response = env.client.get(f"{_URL}/schema", headers=env.headers("editor"))
    denied = env.client.get(f"{_URL}/schema", headers=env.headers("reader"))

    assert response.status_code == 200
    groups = response.json()["groups"]
    assert [(group["name"], group["title"]) for group in groups] == [
        ("retrieval", "Retrieval"),
        ("parsing", "Parsing"),
        ("chunking", "Chunking"),
        ("ingestion", "Ingestion"),
        ("models", "Models"),
    ]
    entries = {entry["key"]: entry for group in groups for entry in group["settings"]}
    listed = [entry["key"] for group in groups for entry in group["settings"]]
    assert sorted(listed) == sorted(SettingsRegistry.default().keys())
    assert entries["chunking.max_tokens"] == {
        "key": "chunking.max_tokens",
        "type": "integer",
        "default": 1200,
        "min": 64,
        "max": 4096,
        "levels": ["tenant", "project", "connection"],
        "applies": "next_ingestion",
        "description": entries["chunking.max_tokens"]["description"],
    }
    assert entries["parsing.pdf_parser"]["enum"] == ["liteparse", "docling"]
    assert entries["parsing.pdf_parser"]["type"] == "enum"
    assert not {"min", "max"} & set(entries["retrieval.rerank"])
    assert entries["models.embedding"]["type"] == "model_ref"
    assert all(entry["description"] for entry in entries.values())
    assert "enum" not in entries["chunking.max_tokens"]
    assert denied.status_code == 403
    assert _SECRET_VALUE not in response.text + denied.text
