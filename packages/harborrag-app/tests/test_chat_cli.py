"""CLI contract tests for one-shot chat completion."""

from __future__ import annotations

import json

from app_test_fixtures import MockAppService

from harborrag_app.cli import main as cli
from harborrag_app.cli import runner as cli_runner
from harborrag_runtime.chat import ChatPrompt


def test_chat_cli_forwards_model_prompt_and_generation_controls(monkeypatch, capsys) -> None:
    service = MockAppService()
    monkeypatch.setattr(cli_runner, "runtime_app_service", lambda: service)

    exit_code = cli.main(
        [
            "chat",
            "Explain HarborRAG",
            "--tenant",
            "ACME",
            "--system",
            "Use plain language.",
            "--prompt",
            "concise",
            "--model",
            "primary",
            "--temperature",
            "0.1",
            "--max-tokens",
            "200",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["data"]["message"]["content"] == "Harbor response"
    call = service.chat_calls[0]
    request = call["request"]
    assert call["tenant_id"] == "ACME"
    assert call["principal_id"] == "harborrag-cli"
    assert call["prompt"] is ChatPrompt.CONCISE
    assert request.logical_model == "primary"
    assert request.temperature == 0.1
    assert request.max_tokens == 200
    assert [message.role.value for message in request.messages] == ["system", "user"]


def test_chat_cli_renders_the_assistant_message(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    exit_code = cli.main(["--no-color", "chat", "Hello"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "HarborChat" in output
    assert "Harbor response" in output
