from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.models.embed import HarborEmbedClientConfig
from harborrag_runtime.topology.embedding_profile import embedding_space_revision

pytestmark = pytest.mark.unit

MODELS_YAML = Path(__file__).resolve().parents[3] / "config" / "models" / "models.yaml"


def _shipped_revision(monkeypatch: pytest.MonkeyPatch, *, line_end: str) -> str:
    for variable, value in {
        "HARBORRAG_CHAT_MODEL": "chat-test",
        "HARBORRAG_CHAT_API_KEY": "chat-secret",
        "HARBORRAG_EMBED_MODEL": "embed-test",
        "HARBORRAG_EMBED_API_KEY": "embed-secret",
    }.items():
        monkeypatch.setenv(variable, value + line_end)
    config = HarborEmbedClientConfig.from_file(MODELS_YAML)
    return embedding_space_revision(config.models[config.default_model])


def test_crlf_env_file_gives_the_same_embedding_space_revision_as_an_lf_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HARBORRAG_EMBED_PROVIDER", raising=False)
    monkeypatch.delenv("HARBORRAG_URL", raising=False)

    lf = _shipped_revision(monkeypatch, line_end="")
    crlf = _shipped_revision(monkeypatch, line_end="\r")

    assert crlf == lf
