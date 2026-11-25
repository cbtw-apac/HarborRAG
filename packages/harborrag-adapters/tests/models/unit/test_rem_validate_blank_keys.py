from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.models.cli import main
from harborrag_adapters.models.runtime import require_set_variables

pytestmark = pytest.mark.unit

SHIPPED_CATALOG = Path(__file__).resolve().parents[5] / "config" / "models" / "models.yaml"


@pytest.fixture(autouse=True)
def catalog_environment(monkeypatch: pytest.MonkeyPatch) -> None:
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


def _validate(family: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    status = main(["validate", str(SHIPPED_CATALOG), "--family", family])
    captured = capsys.readouterr()
    return status, (captured.err or captured.out).strip()


@pytest.mark.parametrize("blank", ["", "  ", "\r"])
def test_validate_refuses_a_blank_chat_key_as_doctor_does(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], blank: str
) -> None:
    monkeypatch.setenv("HARBORRAG_CHAT_API_KEY", blank)

    assert _validate("chat", capsys) == (
        2,
        "configuration error: HARBORRAG_CHAT_API_KEY is blank",
    )


@pytest.mark.parametrize("blank", ["", "  "])
def test_validate_refuses_a_named_reranker_with_a_blank_key_as_doctor_does(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], blank: str
) -> None:
    monkeypatch.setenv("HARBORRAG_RERANKER_MODEL", "demo-rerank")
    monkeypatch.setenv("HARBORRAG_RERANKER_API_KEY", blank)

    assert _validate("rerank", capsys) == (
        2,
        "configuration error: HARBORRAG_RERANKER_API_KEY is empty, and the rerank model needs it",
    )


def test_validate_scans_only_the_selected_family(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HARBORRAG_EMBED_API_KEY", "")
    monkeypatch.setenv("HARBORRAG_RERANKER_MODEL", "demo-rerank")
    monkeypatch.setenv("HARBORRAG_RERANKER_API_KEY", "")

    assert _validate("chat", capsys) == (
        0,
        f"valid chat configuration: {SHIPPED_CATALOG}",
    )
    assert _validate("embed", capsys) == (
        2,
        "configuration error: HARBORRAG_EMBED_API_KEY is blank",
    )


def test_validate_accepts_a_named_reranker_with_its_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HARBORRAG_RERANKER_MODEL", "demo-rerank")
    monkeypatch.setenv("HARBORRAG_RERANKER_API_KEY", "demo-rerank-key")

    assert _validate("rerank", capsys)[0] == 0


def test_blank_variable_scan_skips_defaults_and_names_every_blank_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEMO_SET", "value")
    monkeypatch.setenv("DEMO_BLANK_B", " ")
    monkeypatch.setenv("DEMO_BLANK_A", "")
    monkeypatch.delenv("DEMO_DEFAULTED", raising=False)
    values = {
        "set": "${DEMO_SET}",
        "defaulted": "${DEMO_DEFAULTED:-}",
        "nested": [{"key": "Bearer ${DEMO_BLANK_B}"}, ("${DEMO_BLANK_A}",)],
    }

    with pytest.raises(ValueError, match="^DEMO_BLANK_A, DEMO_BLANK_B is blank$"):
        require_set_variables(values)
    require_set_variables({"set": "${DEMO_SET}", "defaulted": "${DEMO_DEFAULTED:-}"})
