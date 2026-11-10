"""A wildcard credential without ``X-Tenant-Id`` acts in the tenant ``HARBORRAG_TENANT_ID`` names."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from app_test_connections import connection_world
from fastapi.testclient import TestClient
from test_api_admin_ingestion_reads import _App, _Runs

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_core.ingestion import IngestionTaskState

_DONE = IngestionTaskState.COMPLETED


def _client(monkeypatch: pytest.MonkeyPatch, *, tenant_id: str | None) -> Callable[[], TestClient]:
    runs = _Runs()
    runs.add("ing_acme", tenant="acme", source_key="x/y", status=_DONE, age=0)
    runs.add("ing_default", tenant="DEFAULT", source_key="x/y", status=_DONE, age=1)
    service = _App(connection_world([]), runs)
    if tenant_id is not None:
        service.default_tenant_id = tenant_id  # type: ignore[attr-defined]  # service under test
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    return lambda: TestClient(create_fastapi_app(ApiSettings(auth_mode="none")))


def test_unauthenticated_app_acts_in_the_configured_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(monkeypatch, tenant_id="acme")() as client:
        me = client.get("/v1/me")
        runs = client.get("/v1/admin/ingestions")

    assert me.json()["tenant_id"] == "acme"
    assert [item["ingestion_id"] for item in runs.json()["items"]] == ["ing_acme"]


def test_unauthenticated_app_acts_in_default_when_no_tenant_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _client(monkeypatch, tenant_id=None)() as client:
        me = client.get("/v1/me")
        runs = client.get("/v1/admin/ingestions")

    assert me.json()["tenant_id"] == "DEFAULT"
    assert [item["ingestion_id"] for item in runs.json()["items"]] == ["ing_default"]


def test_header_still_overrides_the_configured_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(monkeypatch, tenant_id="acme")() as client:
        me = client.get("/v1/me", headers={"X-Tenant-Id": "other"})

    assert me.json()["tenant_id"] == "other"
