from __future__ import annotations

import pytest

from harborrag_adapters.models.common.security import SecretReference
from harborrag_adapters.models.rerank import (
    HarborRerankClientConfig,
    HarborRerankProvider,
)
from harborrag_adapters.models.rerank.validation import validate_rerank_configuration
from harborrag_core.models.errors import HarborRerankConfigurationError

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def _document(provider: str, **deployment):
    return {
        "rerank": {
            "default_model": "primary",
            "models": {
                "primary": {
                    "provider": provider,
                    "model": deployment.pop("model", "provider/rerank-model"),
                    **deployment,
                }
            },
        }
    }


def test_cohere_config_and_secret_repr() -> None:
    config = HarborRerankClientConfig.from_dict(
        _document("cohere", api_key="super-secret", model="cohere/rerank-v3.5")
    )
    deployment = config.models["primary"].deployments[0]
    assert deployment.provider is HarborRerankProvider.COHERE
    assert "super-secret" not in repr(config)


def test_bedrock_rerank_requires_region() -> None:
    incomplete = HarborRerankClientConfig.from_dict(
        _document("bedrock", model="bedrock/amazon.rerank-v1:0")
    )
    with pytest.raises(HarborRerankConfigurationError, match="aws_region_name"):
        validate_rerank_configuration(incomplete)
    config = HarborRerankClientConfig.from_dict(
        _document(
            "bedrock",
            model="bedrock/amazon.rerank-v1:0",
            aws_region_name="us-west-2",
            allow_ambient_credentials=True,
        )
    )
    assert config.models["primary"].deployments[0].aws_region_name == "us-west-2"


@pytest.mark.parametrize(
    ("provider", "extra"),
    [
        (
            "infinity",
            {"api_base": "http://localhost:7997", "model": "infinity/bge-reranker"},
        ),
        (
            "vllm",
            {"api_base": "http://localhost:8000", "model": "hosted_vllm/bge-reranker"},
        ),
        (
            "jina_ai",
            {"api_key": "key", "model": "jina_ai/jina-reranker-v2-base-multilingual"},
        ),
        ("voyage", {"api_key": "key", "model": "voyage/rerank-2.5"}),
    ],
)
def test_representative_rerank_provider_configs(provider: str, extra: dict[str, str]) -> None:
    config = HarborRerankClientConfig.from_dict(_document(provider, **extra))
    assert config.models["primary"].deployments[0].provider.value == provider


def test_environment_expansion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARBOR_RERANK_KEY", "env-secret")
    config = HarborRerankClientConfig.from_dict(_document("cohere", api_key="${HARBOR_RERANK_KEY}"))
    assert config.models["primary"].deployments[0].api_key.get_secret_value() == "env-secret"


def test_secret_reference_resolution() -> None:
    class Resolver:
        def resolve(self, reference: SecretReference) -> str:
            return f"resolved:{reference.uri}"

    config = HarborRerankClientConfig.from_dict(
        _document("cohere", api_key="secret://vault/rerank/key"),
        secret_resolver=Resolver(),
    )
    assert (
        config.models["primary"].deployments[0].api_key.get_secret_value()
        == "resolved:secret://vault/rerank/key"
    )


def test_alias_and_fallback_validation() -> None:
    config = HarborRerankClientConfig.from_dict(
        {
            "rerank": {
                "default_model": "default-reranker",
                "models": {
                    "primary": {
                        "aliases": ["default-reranker"],
                        "fallbacks": ["backup"],
                        "provider": "cohere",
                        "model": "cohere/rerank-v3.5",
                        "api_key": "key",
                    },
                    "backup": {
                        "provider": "voyage",
                        "model": "voyage/rerank-2.5",
                        "api_key": "key2",
                    },
                },
            }
        }
    )
    assert config.model_for()[0] == "primary"
