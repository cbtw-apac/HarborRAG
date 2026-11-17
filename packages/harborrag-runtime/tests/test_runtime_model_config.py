from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.models.chat import HarborChatClientConfig
from harborrag_adapters.models.embed import HarborEmbedClientConfig
from harborrag_adapters.models.rerank import HarborRerankClientConfig
from harborrag_core.models.embed import EmbeddingPurpose
from harborrag_runtime.config.models import has_rerank_model

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
MODELS_YAML = REPO_ROOT / "config" / "models" / "models.yaml"


@pytest.fixture(autouse=True)
def gateway_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARBORRAG_URL", "https://gateway.example.com")
    monkeypatch.setenv("HARBORRAG_API_KEY", "test-secret")
    monkeypatch.setenv("HARBORRAG_CHAT_MODEL", "chat-test")
    monkeypatch.setenv("HARBORRAG_EMBED_MODEL", "embed-test")
    monkeypatch.setenv("HARBORRAG_RERANKER_MODEL", "rerank-test")


def test_repository_chat_config_supports_agent_tool_execution() -> None:
    config = HarborChatClientConfig.from_file(MODELS_YAML)
    deployment = config.models[config.default_model].deployments[0]

    assert deployment.capabilities.tools is True
    assert deployment.capabilities.parallel_tools is True


def test_repository_embedding_config_supports_index_and_query_purposes() -> None:
    config = HarborEmbedClientConfig.from_file(MODELS_YAML)
    deployment = config.models[config.default_model].deployments[0]

    assert deployment.capabilities.purpose is True
    assert deployment.capabilities.supported_purposes == {
        EmbeddingPurpose.QUERY,
        EmbeddingPurpose.DOCUMENT,
    }


def test_repository_rerank_config_reaches_the_gateway() -> None:
    config = HarborRerankClientConfig.from_file(MODELS_YAML)
    deployment = config.models[config.default_model].deployments[0]

    assert has_rerank_model(MODELS_YAML)
    assert deployment.model == "rerank-test"
    assert deployment.api_base == "https://gateway.example.com"
