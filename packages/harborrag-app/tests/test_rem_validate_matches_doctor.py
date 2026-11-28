"""`harborrag doctor` and `python -m harborrag_adapters.models validate` refuse the same keys."""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.models.cli import main as validate_models
from harborrag_app.cli.doctor.environment import models_check

pytestmark = pytest.mark.unit

_SHIPPED_CATALOG = Path(__file__).resolve().parents[3] / "config" / "models" / "models.yaml"


@pytest.fixture(autouse=True)
def _catalog_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARBORRAG_CHAT_MODEL", "demo-chat")
    monkeypatch.setenv("HARBORRAG_CHAT_API_KEY", "demo-chat-key")
    monkeypatch.setenv("HARBORRAG_EMBED_MODEL", "demo-embed")
    monkeypatch.setenv("HARBORRAG_EMBED_API_KEY", "demo-embed-key")
    for optional in (
        "HARBORRAG_URL",
        "HARBORRAG_EMBED_PROVIDER",
        "HARBORRAG_RERANKER_MODEL",
        "HARBORRAG_RERANKER_API_KEY",
    ):
        monkeypatch.delenv(optional, raising=False)


@pytest.mark.parametrize(
    ("family", "environment"),
    [
        pytest.param("chat", {"HARBORRAG_CHAT_API_KEY": ""}, id="blank-chat-key"),
        pytest.param(
            "rerank",
            {"HARBORRAG_RERANKER_MODEL": "demo-rerank", "HARBORRAG_RERANKER_API_KEY": ""},
            id="named-reranker-blank-key",
        ),
    ],
)
def test_validate_fails_with_the_message_doctor_reports(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    family: str,
    environment: dict[str, str],
) -> None:
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    check = models_check(_SHIPPED_CATALOG)
    status = validate_models(["validate", str(_SHIPPED_CATALOG), "--family", family])
    message = capsys.readouterr().err.strip().removeprefix("configuration error: ")

    assert check.status == "fail"
    assert status == 2
    assert check.detail.removeprefix(f"{_SHIPPED_CATALOG}: ") == message
