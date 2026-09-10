"""CLI contract tests for one-shot chat completion."""

from __future__ import annotations

import json

from app_test_fixtures import MockAppService

from harborrag_app.cli import main as cli
from harborrag_app.cli import runner as cli_runner
from harborrag_runtime.chat import ChatPrompt


def test_chat_cli_creates_session_and_uses_default_prompt(monkeypatch, capsys) -> None:
    service = MockAppService()
    monkeypatch.setattr(cli_runner, "runtime_app_service", lambda: service)

    exit_code = cli.main(
        [
            "chat",
            "Explain HarborRAG",
            "--tenant",
            "ACME",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["data"]["message"]["content"] == "Harbor response"
    call = service.chat_calls[0]
    assert call["tenant_id"] == "ACME"
    assert call["principal_id"] == "harborrag-cli"
    assert call["system"] is ChatPrompt.DEFAULT
    assert str(call["session_id"]).startswith("session-")
    assert call["query"] == "Explain HarborRAG"


def test_chat_cli_renders_the_assistant_message(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    exit_code = cli.main(["--no-color", "chat", "Hello"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "HarborChat" in output
    assert "Harbor response" in output


def test_chat_cli_forwards_the_project_scope(monkeypatch, capsys) -> None:
    service = MockAppService()
    monkeypatch.setattr(cli_runner, "runtime_app_service", lambda: service)

    exit_code = cli.main(["chat", "Hello", "--project", "proj-1", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["data"]["project_id"] == "proj-1"
    assert service.chat_calls[0]["project_id"] == "proj-1"
    assert service.chat_calls[0]["user_id"] is None
