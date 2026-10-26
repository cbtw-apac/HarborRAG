"""Public transports preserve explicit semantic-mode selection."""

from __future__ import annotations

from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_app.cli import main as cli
from harborrag_app.cli import runner
from harborrag_runtime.sdk import RetrievalMode


def test_cli_forwards_opt_in_semantic_mode(monkeypatch, capsys):
    service = MockAppService()
    monkeypatch.setattr(runner, "runtime_app_service", lambda: service)
    assert cli.main(["retrieve", "dependencies", "--mode", "local_semantic", "--json"]) == 0
    capsys.readouterr()
    assert service.retrieval_calls[0]["mode"] == RetrievalMode.LOCAL_SEMANTIC


def test_http_forwards_mode_and_rejects_unimplemented_global_mode(monkeypatch):
    service = MockAppService()
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        response = client.post(
            "/v1/retrieval/vector", json={"query": "dependencies", "mode": "local_semantic"}
        )
        assert response.status_code == 200
        assert service.retrieval_calls[0]["mode"] == RetrievalMode.LOCAL_SEMANTIC
        unsupported = client.post(
            "/v1/retrieval/vector", json={"query": "themes", "mode": "global"}
        )
        assert unsupported.status_code == 422
