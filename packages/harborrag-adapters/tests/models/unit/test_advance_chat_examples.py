from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.models.chat import HarborChatClientConfig
from harborrag_adapters.models.chat.validation import validate_chat_configuration
from harborrag_adapters.models.embed import HarborEmbedClientConfig
from harborrag_adapters.models.embed.validation import validate_embed_configuration
from harborrag_adapters.models.rerank import HarborRerankClientConfig
from harborrag_adapters.models.rerank.validation import validate_rerank_configuration
from harborrag_adapters.models.runtime.loading import load_config_document

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]

ADVANCE_CHAT_DIR = Path(__file__).resolve().parents[5] / "config" / "advance_chat"
EXAMPLE_FILES = sorted(ADVANCE_CHAT_DIR.glob("*.yaml"))

_EXAMPLE_ENVIRONMENT = {
    "OPENAI_API_KEY": "openai-secret",
    "COHERE_API_KEY": "cohere-secret",
    "LITELLM_PROXY_API_BASE": "https://proxy.example.test",
    "LITELLM_PROXY_API_KEY": "proxy-secret",
}


def _load_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _EXAMPLE_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)


def test_advance_chat_examples_directory_is_covered() -> None:
    assert EXAMPLE_FILES, f"no example configs found under {ADVANCE_CHAT_DIR}"


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=lambda path: path.name)
def test_advance_chat_example_loads_through_real_config_loader(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every shipped example must validate through ``from_file`` for each declared family."""

    _load_environment(monkeypatch)
    document = load_config_document(path)

    chat = HarborChatClientConfig.from_file(path)
    validate_chat_configuration(chat)
    assert chat.default_model in chat.models
    assert "openai-secret" not in repr(chat)
    assert "proxy-secret" not in repr(chat)

    if "embed" in document:
        embed = HarborEmbedClientConfig.from_file(path)
        validate_embed_configuration(embed)
    if "rerank" in document:
        rerank = HarborRerankClientConfig.from_file(path)
        validate_rerank_configuration(rerank)


@pytest.mark.parametrize(
    "name",
    ["chat.direct.example.yaml", "chat.proxy.example.yaml", "models.distributed.example.yaml"],
)
def test_advance_chat_examples_declare_reasoning_capability(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The examples advertise ``reasoning`` so ``reasoning_effort`` requests are accepted."""

    _load_environment(monkeypatch)
    chat = HarborChatClientConfig.from_file(ADVANCE_CHAT_DIR / name)
    deployment = chat.models[chat.default_model].deployments[0]
    assert deployment.capabilities.reasoning is True
    assert deployment.capabilities.reasoning_content is False
