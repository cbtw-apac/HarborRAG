"""Public transports preserve explicit semantic-mode selection."""

from __future__ import annotations

from app_test_fixtures import MockAppService

from harborrag_app.cli import main as cli
from harborrag_app.cli import runner
from harborrag_runtime.sdk import RetrievalMode


def test_cli_forwards_opt_in_semantic_mode(monkeypatch, capsys):
    service = MockAppService()
    monkeypatch.setattr(runner, "runtime_app_service", lambda: service)
    assert cli.main(["retrieve", "dependencies", "--mode", "local_semantic", "--json"]) == 0
    capsys.readouterr()
    assert service.retrieval_calls[0]["mode"] == RetrievalMode.LOCAL_SEMANTIC
