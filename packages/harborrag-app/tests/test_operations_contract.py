"""Every ``/v1`` route takes its role, response model and flags from ``OPERATIONS``."""

from __future__ import annotations

import builtins
import importlib
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient
from test_api_rate_limits import _SECRET, _bearer
from test_api_rate_limits import client as client  # noqa: F401  # fixture
from test_api_rate_limits import clock as clock  # noqa: F401  # fixture
from test_api_rate_limits import service as service  # noqa: F401  # fixture

from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.auth.access import api_routes, minimum_role
from harborrag_app.api.settings import ApiSettings
from harborrag_app.operations.contract import OPERATIONS, dump_body, route_kwargs

pytestmark = pytest.mark.unit


def _v1_routes() -> list[Any]:
    app = create_fastapi_app(ApiSettings(auth_mode="none"))
    return [
        route
        for route in api_routes(app.routes)
        if (getattr(route, "path_format", None) or route.path).startswith("/v1/")
    ]


def _route(route: Any) -> Any:
    return getattr(route, "original_route", route)


def test_the_table_names_exactly_the_v1_routes() -> None:
    names = {_route(route).name for route in _v1_routes()}

    assert set(OPERATIONS) == names
    assert len(names) == 30


def test_every_route_matches_its_table_entry() -> None:
    for route in _v1_routes():
        declared = _route(route)
        spec = OPERATIONS[declared.name]

        assert declared.response_model == spec.response_model, declared.name
        assert declared.status_code == spec.status_code, declared.name
        assert declared.response_model_by_alias is spec.by_alias, declared.name
        assert declared.response_model_exclude_unset is spec.exclude_unset, declared.name
        assert minimum_role(route) == spec.role, declared.name


def test_route_kwargs_carry_the_declared_flags() -> None:
    assert route_kwargs("list_failures") == {
        "response_model": OPERATIONS["list_failures"].response_model,
        "status_code": 200,
        "response_model_by_alias": True,
        "response_model_exclude_unset": True,
    }
    assert "operation_id" not in route_kwargs("me")


def test_schema_modules_import_without_fastapi(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def blocked(name: str, *args: object, **kwargs: object) -> object:
        if name.split(".")[0] in {"fastapi", "starlette"}:
            raise ModuleNotFoundError(f"No module named {name!r} (blocked by test)")
        return real_import(name, *args, **kwargs)

    for name in (
        "harborrag_app.api.v1.ingestion.schemas",
        "harborrag_app.api.v1.chat.schemas",
        "harborrag_app.api.v1.me_schemas",
        "harborrag_app.operations.contract",
    ):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(builtins, "__import__", blocked)

    for name in (
        "harborrag_app.api.v1.ingestion.schemas",
        "harborrag_app.api.v1.chat.schemas",
        "harborrag_app.api.v1.me_schemas",
        "harborrag_app.operations.contract",
    ):
        assert importlib.import_module(name) is not None


def test_dump_body_leaves_out_fields_the_route_leaves_out() -> None:
    body = dump_body("list_failures", {"items": [], "counts": {}})

    assert isinstance(body, dict)
    assert "total" not in body


def test_dump_body_serializes_with_aliases() -> None:
    body = dump_body("list_source_types", {"items": []})

    assert body == {"items": []}


def test_me_without_a_tenant_header_answers_for_a_multi_tenant_credential(
    client: TestClient,
) -> None:
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": "u_alex",
            "role": "reader",
            "tenants": ["acme", "beta"],
            "iat": now - timedelta(seconds=1),
            "exp": now + timedelta(minutes=5),
            "iss": "harborrag",
            "aud": "harborrag-api",
        },
        _SECRET,
        algorithm="HS256",
    )

    response = client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["tenant_id"] is None


def test_chat_completions_charge_the_chat_bucket(client: TestClient) -> None:
    statuses = [
        client.post("/v1/chat/completions", headers=_bearer("reader"), json={"message": "hi"})
        for _ in range(3)
    ]

    assert [response.status_code for response in statuses] == [200, 200, 429]
