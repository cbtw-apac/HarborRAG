"""`harborrag doctor` fails a named reranker whose key is blank, as the servers do."""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_app.cli.doctor.environment import models_check

pytestmark = pytest.mark.unit

_SHIPPED_CATALOG = Path(__file__).resolve().parents[3] / "config" / "models" / "models.yaml"
_PLANTED = "demo-ambient-key-0000"


@pytest.fixture(autouse=True)
def _catalog_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARBORRAG_CHAT_MODEL", "demo-chat")
    monkeypatch.setenv("HARBORRAG_CHAT_API_KEY", "demo-chat-key")
    monkeypatch.setenv("HARBORRAG_EMBED_MODEL", "demo-embed")
    monkeypatch.setenv("HARBORRAG_EMBED_API_KEY", "demo-embed-key")
    monkeypatch.setenv("COHERE_API_KEY", _PLANTED)
    monkeypatch.delenv("HARBORRAG_RERANKER_MODEL", raising=False)
    monkeypatch.delenv("HARBORRAG_RERANKER_API_KEY", raising=False)


def test_models_check_fails_a_named_reranker_with_a_blank_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARBORRAG_RERANKER_MODEL", "demo-rerank")
    monkeypatch.setenv("HARBORRAG_RERANKER_API_KEY", "  ")

    check = models_check(_SHIPPED_CATALOG)

    assert check.status == "fail"
    assert "HARBORRAG_RERANKER_API_KEY" in check.detail
    assert _PLANTED not in check.detail


def test_models_check_passes_a_named_reranker_with_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARBORRAG_RERANKER_MODEL", "demo-rerank")
    monkeypatch.setenv("HARBORRAG_RERANKER_API_KEY", "demo-rerank-key")

    assert models_check(_SHIPPED_CATALOG).status == "ok"


def test_models_check_passes_when_reranking_is_off() -> None:
    assert models_check(_SHIPPED_CATALOG).status == "ok"
