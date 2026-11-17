"""CLI contract tests for one-shot chat completion."""

from __future__ import annotations

import asyncio
import json

import pytest
from app_test_fixtures import MockAppService

from harborrag_app.cli import main as cli
from harborrag_app.cli import runner as cli_runner
from harborrag_core.domain.identity import DEFAULT_USER
from harborrag_runtime.chat import ChatPrompt
from harborrag_runtime.memory import ConversationIdentity

# Every command below goes through the CLI's project gate; give each test its own
# project instead of inheriting whatever directory pytest was started from.
pytestmark = pytest.mark.usefixtures("cli_project")


def _service() -> MockAppService:
    service = MockAppService()
    service.catalog.add("proj-1", "proj-1/docs", ("scope-docs",), documents=2)
    return service


def test_chat_cli_creates_session_and_uses_default_prompt(monkeypatch, capsys) -> None:
    service = _service()
    monkeypatch.setattr(cli_runner, "runtime_app_service", lambda: service)

    exit_code = cli.main(["chat", "Explain HarborRAG", "--tenant", "ACME", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["data"]["answer"].startswith("Two candidates match")
    assert payload["data"]["mode"] == "classic"
    assert str(payload["data"]["session_id"]).startswith("ses_")
    request = service.chat.requests[0]
    assert request.metadata.tenant_id == "ACME"
    assert request.messages[-1].content.endswith("Question: Explain HarborRAG")
    assert service.chat.prompts == [ChatPrompt.DEFAULT]


def test_chat_cli_renders_the_answer(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_runner, "runtime_app_service", _service)

    exit_code = cli.main(["--no-color", "chat", "Hello"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "HarborChat" in output
    assert "classic" in output
    assert "Two candidates match" in output


def test_chat_cli_creates_the_session_in_the_project(monkeypatch, capsys) -> None:
    service = _service()
    monkeypatch.setattr(cli_runner, "runtime_app_service", lambda: service)

    exit_code = cli.main(["chat", "Hello", "--project", "proj-1", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    session_id = str(payload["data"]["session_id"])
    owner = ConversationIdentity("DEFAULT", "harborrag-cli", session_id, DEFAULT_USER)
    summary = asyncio.run(service.conversations.get_conversation(owner))
    assert summary is not None
    assert summary.project_id == "proj-1"
    assert service.retrieval.request.filters == {"source_scope_id": ["scope-docs"]}  # type: ignore[union-attr]
