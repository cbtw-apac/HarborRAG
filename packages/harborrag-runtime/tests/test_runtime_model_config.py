from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.models.chat import HarborChatClientConfig
from harborrag_adapters.models.embed import HarborEmbedClient, HarborEmbedClientConfig
from harborrag_adapters.models.rerank import HarborRerankClientConfig
from harborrag_adapters.models.rerank.validation import validate_rerank_configuration
from harborrag_core.models.embed import EmbeddingPurpose
from harborrag_core.models.errors import (
    HarborEmbedConfigurationError,
    HarborRerankConfigurationError,
)
from harborrag_runtime.config.models import has_rerank_model

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
MODELS_YAML = REPO_ROOT / "config" / "models" / "models.yaml"
OPENAI_ENDPOINT = "https://api.openai.com/v1"
GATEWAY_URL = "https://gateway.example.com"


@pytest.fixture(autouse=True)
def model_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set what the shipped catalog requires and nothing it treats as optional."""

    monkeypatch.setenv("HARBORRAG_CHAT_MODEL", "chat-test")
    monkeypatch.setenv("HARBORRAG_CHAT_API_KEY", "chat-secret")
    monkeypatch.setenv("HARBORRAG_EMBED_MODEL", "embed-test")
    monkeypatch.setenv("HARBORRAG_EMBED_API_KEY", "embed-secret")
    for optional in (
        "HARBORRAG_URL",
        "HARBORRAG_EMBED_PROVIDER",
        "HARBORRAG_RERANKER_MODEL",
        "HARBORRAG_RERANKER_API_KEY",
    ):
        monkeypatch.delenv(optional, raising=False)


def _chat_deployment(path: Path):
    config = HarborChatClientConfig.from_file(path)
    return config.models[config.default_model].deployments[0]


def _embedding_deployment(path: Path):
    config = HarborEmbedClientConfig.from_file(path)
    return config.models[config.default_model].deployments[0]


def test_repository_chat_config_supports_agent_tool_execution() -> None:
    deployment = _chat_deployment(MODELS_YAML)

    assert deployment.capabilities.tools is True
    assert deployment.capabilities.parallel_tools is True


def test_repository_embedding_config_supports_index_and_query_purposes() -> None:
    deployment = _embedding_deployment(MODELS_YAML)

    assert deployment.capabilities.purpose is True
    assert deployment.capabilities.supported_purposes == {
        EmbeddingPurpose.QUERY,
        EmbeddingPurpose.DOCUMENT,
    }


def test_repository_catalog_calls_openai_when_no_endpoint_is_set() -> None:
    assert _chat_deployment(MODELS_YAML).api_base == OPENAI_ENDPOINT
    assert _embedding_deployment(MODELS_YAML).api_base == OPENAI_ENDPOINT


def test_repository_catalog_sends_openai_style_embeddings_by_default() -> None:
    embedding = _embedding_deployment(MODELS_YAML)

    assert embedding.provider.value == "openai_compatible"
    assert embedding.custom_llm_provider == "litellm_proxy"


def test_repository_catalog_keeps_one_key_per_family_behind_one_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARBORRAG_URL", GATEWAY_URL)

    chat = _chat_deployment(MODELS_YAML)
    embedding = _embedding_deployment(MODELS_YAML)

    assert (chat.api_base, chat.api_key.get_secret_value()) == (GATEWAY_URL, "chat-secret")
    assert (embedding.api_base, embedding.api_key.get_secret_value()) == (
        GATEWAY_URL,
        "embed-secret",
    )


def test_repository_catalog_names_the_embedding_space_after_the_model() -> None:
    config = HarborEmbedClientConfig.from_file(MODELS_YAML)

    assert config.models[config.default_model].embedding_space == "embed-test"


def test_repository_catalog_ships_without_a_rerank_model() -> None:
    assert has_rerank_model(MODELS_YAML) is False


@pytest.mark.parametrize("reranker_key", [None, "", "  ", "\r"])
def test_repository_catalog_refuses_a_reranker_without_its_key(
    monkeypatch: pytest.MonkeyPatch, reranker_key: str | None
) -> None:
    monkeypatch.setenv("HARBORRAG_RERANKER_MODEL", "rerank-test")
    if reranker_key is not None:
        monkeypatch.setenv("HARBORRAG_RERANKER_API_KEY", reranker_key)

    with pytest.raises(ValueError, match="^HARBORRAG_RERANKER_API_KEY is empty, and the rerank"):
        has_rerank_model(MODELS_YAML)
    with pytest.raises(HarborRerankConfigurationError, match="empty api_key"):
        validate_rerank_configuration(HarborRerankClientConfig.from_file(MODELS_YAML))


def _catalog_with_an_optional_key(tmp_path: Path, provider: str) -> Path:
    catalog = tmp_path / "models.yaml"
    catalog.write_text(
        "rerank:\n"
        "  default_model: primary\n"
        "  models:\n"
        "    primary:\n"
        "      deployments:\n"
        "        - name: rerank\n"
        f"          provider: {provider}\n"
        "          model: rerank-test\n"
        "          api_base: http://127.0.0.1:8080/v1\n"
        "          api_key: ${RERANK_TEST_KEY:-}\n",
        encoding="utf-8",
    )
    return catalog


@pytest.mark.parametrize(
    "provider",
    ["vllm", "infinity", "litellm_proxy", "cohere", "${RERANK_TEST_PROVIDER:-cohere}"],
)
def test_rerank_provider_refuses_an_empty_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    monkeypatch.delenv("RERANK_TEST_KEY", raising=False)
    monkeypatch.delenv("RERANK_TEST_PROVIDER", raising=False)
    catalog = _catalog_with_an_optional_key(tmp_path, provider)

    with pytest.raises(ValueError, match="RERANK_TEST_KEY is empty"):
        has_rerank_model(catalog)
    with pytest.raises(HarborRerankConfigurationError, match="empty api_key"):
        validate_rerank_configuration(HarborRerankClientConfig.from_file(catalog))


def test_rerank_provider_that_needs_a_key_accepts_a_set_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RERANK_TEST_KEY", "rerank-secret")

    assert has_rerank_model(_catalog_with_an_optional_key(tmp_path, "cohere")) is True


def test_unknown_rerank_provider_is_left_to_the_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RERANK_TEST_KEY", "rerank-secret")

    assert has_rerank_model(_catalog_with_an_optional_key(tmp_path, "not-a-provider")) is True


def _catalog_with_an_optional_reranker(tmp_path: Path) -> Path:
    catalog = tmp_path / "models.yaml"
    catalog.write_text(
        "rerank:\n"
        "  default_model: primary\n"
        "  models:\n"
        "    primary:\n"
        "      deployments:\n"
        "        - name: rerank\n"
        "          provider: litellm_proxy\n"
        "          model: ${HARBORRAG_RERANKER_MODEL:-}\n",
        encoding="utf-8",
    )
    return catalog


@pytest.mark.parametrize("reranker_model", [None, "", "  "])
def test_rerank_section_that_names_no_model_is_not_a_rerank_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reranker_model: str | None
) -> None:
    if reranker_model is not None:
        monkeypatch.setenv("HARBORRAG_RERANKER_MODEL", reranker_model)

    assert has_rerank_model(_catalog_with_an_optional_reranker(tmp_path)) is False


def test_rerank_section_that_names_a_model_is_a_rerank_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARBORRAG_RERANKER_MODEL", "rerank-test")

    assert has_rerank_model(_catalog_with_an_optional_reranker(tmp_path)) is True


def test_repository_catalog_serves_a_gateway_with_cohere_embeddings_and_a_reranker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARBORRAG_URL", GATEWAY_URL)
    monkeypatch.setenv("HARBORRAG_EMBED_PROVIDER", "cohere")
    monkeypatch.setenv("HARBORRAG_RERANKER_MODEL", "rerank-test")
    monkeypatch.setenv("HARBORRAG_RERANKER_API_KEY", "rerank-secret")

    chat = _chat_deployment(MODELS_YAML)
    embedding = _embedding_deployment(MODELS_YAML)
    rerank_config = HarborRerankClientConfig.from_file(MODELS_YAML)
    rerank = rerank_config.models[rerank_config.default_model].deployments[0]

    assert [family.api_key.get_secret_value() for family in (chat, embedding, rerank)] == [
        "chat-secret",
        "embed-secret",
        "rerank-secret",
    ]
    assert [family.api_base for family in (chat, embedding, rerank)] == [GATEWAY_URL] * 3
    assert (embedding.provider.value, embedding.custom_llm_provider) == ("cohere", "litellm_proxy")
    assert has_rerank_model(MODELS_YAML)
    assert rerank.model == "rerank-test"


@pytest.mark.parametrize("provider", ["openai", "openai_compatible", "cohere"])
def test_repository_catalog_accepts_each_embedding_provider_it_documents(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    monkeypatch.setenv("HARBORRAG_EMBED_PROVIDER", provider)

    config = HarborEmbedClientConfig.from_file(MODELS_YAML)
    HarborEmbedClient.from_config(config)

    assert config.models[config.default_model].deployments[0].provider.value == provider


def test_repository_catalog_refuses_an_embedding_provider_it_cannot_serve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARBORRAG_EMBED_PROVIDER", "ollama")

    with pytest.raises(HarborEmbedConfigurationError, match="'ollama' is not allowed"):
        HarborEmbedClient.from_config(HarborEmbedClientConfig.from_file(MODELS_YAML))
