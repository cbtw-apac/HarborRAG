"""CLI contract tests for one-shot chat completion."""

from __future__ import annotations

import json

from app_test_fixtures import MockAppService

from harborrag_app.cli import main as cli
from harborrag_app.cli import runner as cli_runner
from harborrag_runtime.chat import ChatPrompt


def test_chat_cli_forwards_tenant_and_system_prompt(monkeypatch, capsys) -> None:
    service = MockAppService()
    monkeypatch.setattr(cli_runner, "runtime_app_service", lambda: service)

    exit_code = cli.main(
        [
            "chat",
            "Explain HarborRAG",
            "--tenant",
            "ACME",
            "--system",
            "concise",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["data"]["message"]["content"] == "Harbor response"
    call = service.chat_calls[0]
    assert call["tenant_id"] == "ACME"
    assert call["principal_id"] == "harborrag-cli"
    assert call["system"] is ChatPrompt.CONCISE
    assert call["query"] == "Explain HarborRAG"


def test_chat_cli_renders_the_assistant_message(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    exit_code = cli.main(["--no-color", "chat", "Hello"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "HarborChat" in output
    assert "Harbor response" in output
