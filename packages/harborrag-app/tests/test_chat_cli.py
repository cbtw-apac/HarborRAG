"""``harborrag chat``: the request the flags build, the answer's table and the failure codes."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest
from cli_fakes import FakeOperations, serving

from harborrag_app.api.v1.chat.dispatch import ModeUnavailableError
from harborrag_app.api.v1.chat.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Citation,
    Usage,
)
from harborrag_app.cli import invoke
from harborrag_app.cli import main as cli
from harborrag_app.operations.port import ChatCompletion
from harborrag_core.contracts.errors import HarborNotFoundError

pytestmark = pytest.mark.unit

_ANSWER = ChatCompletionResponse(
    completion_id="msg_1",
    session_id=None,
    trace_id="t" * 32,
    mode="classic",
    answer="Alex Example owns the DEMO-1 rollout [1].",
    citations=[
        Citation(
            n=1,
            chunk_id="c1",
            document_id="d1",
            title="DEMO-1 rollout plan",
            snippet="Alex Example owns the rollout.",
            url="https://example.atlassian.net/browse/DEMO-1",
            page=None,
        )
    ],
    usage=Usage(
        mode="classic",
        reasoning="off",
        steps=1,
        prompt_tokens=10,
        completion_tokens=5,
        reasoning_tokens=0,
        cost_usd=None,
    ),
)


@pytest.fixture(autouse=True)
def _hermetic(cli_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(cli_project)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in ("HARBORRAG_TENANT_ID", "HARBORRAG_OUTPUT", "HARBORRAG_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("HARBORRAG_LOG_LEVEL", raising=False)
    monkeypatch.setattr(logging.getLogger("harborrag"), "handlers", [])


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeOperations:
    operations = FakeOperations(
        results={"create_chat_completion": ChatCompletion(_ANSWER, None, False)}
    )
    monkeypatch.setattr(invoke, "local_operations", serving(operations))
    return operations


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    capsys.readouterr()
    code = cli.main(["chat", *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_chat_json_prints_the_completion_response(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys, "hello", "-o", "json")

    assert code == 0
    assert ChatCompletionResponse.model_validate_json(out) == _ANSWER


def test_chat_table_prints_the_answer_then_numbered_citations(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys, "hello", "-o", "table")

    assert code == 0
    lines = out.splitlines()
    assert lines[0] == _ANSWER.answer
    assert "[1] DEMO-1 rollout plan" in out
    assert "https://example.atlassian.net/browse/DEMO-1" in out
    assert out.index("[1]") > out.index(_ANSWER.answer)


def test_chat_builds_a_non_streaming_request_from_the_flags(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, _ = _run(
        capsys,
        "hello",
        "--session-id",
        "ses_1",
        "--mode",
        "instant",
        "--project-id",
        "engineering",
        "--project-id",
        "default",
        "--connection-ref",
        "engineering/jira-main",
    )

    assert code == 0
    [call] = fake.calls
    assert call.args[1] == ChatCompletionRequest(
        session_id="ses_1",
        message="hello",
        mode="instant",
        project_ids=["engineering", "default"],
        connections=["engineering/jira-main"],
        stream=False,
    )
    assert call.kwargs["idempotency_key"] is None


def test_chat_without_narrowing_sends_none(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(capsys, "hello")

    request = fake.calls[0].args[1]
    assert (request.project_ids, request.connections, request.mode) == (None, None, "classic")


def test_an_unavailable_mode_exits_2(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.errors["create_chat_completion"] = ModeUnavailableError("mode 'advanced' is unavailable")

    code, _, err = _run(capsys, "hello", "--mode", "advanced", "-o", "table")

    assert code == 2
    assert err.startswith("error 422 mode_unavailable: ")


def test_a_blank_message_is_a_usage_error_that_does_not_echo_the_message(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _run(capsys, "   ")

    assert code == 2
    assert "message" in err
    assert fake.calls == []


def test_the_operation_receives_the_trace_id_the_error_envelope_prints(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.errors["create_chat_completion"] = HarborNotFoundError("session ses_x not found")

    code, _, err = _run(capsys, "hello", "--session-id", "ses_x", "-o", "json")

    assert code == 3
    printed = json.loads(err)["error"]["trace_id"]
    assert re.fullmatch(r"[0-9a-f]{32}", printed)
    assert fake.calls[0].kwargs["trace_id"] == printed
