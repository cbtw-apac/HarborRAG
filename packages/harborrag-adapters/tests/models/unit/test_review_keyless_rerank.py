from __future__ import annotations

import pytest

from harborrag_adapters.models.rerank import HarborRerankClientConfig
from harborrag_adapters.models.rerank.validation import validate_rerank_configuration
from harborrag_core.models.errors import HarborRerankConfigurationError

pytestmark = pytest.mark.unit


def _rerank_catalog(provider: str, api_key: str) -> HarborRerankClientConfig:
    custom = {"custom_llm_provider": "cohere"} if provider == "custom" else {}
    return HarborRerankClientConfig.from_dict(
        {
            "rerank": {
                "default_model": "primary",
                "security": {"allow_custom_providers": True},
                "models": {
                    "primary": {
                        "deployments": [
                            {
                                "name": "gateway-rerank",
                                "provider": provider,
                                "model": "rerank-test",
                                "api_base": "https://gateway.example.com/v1",
                                "api_key": api_key,
                                **custom,
                            }
                        ]
                    }
                },
            }
        }
    )


@pytest.mark.parametrize(
    "provider",
    ["litellm_proxy", "vllm", "infinity", "cohere", "jina_ai", "voyage", "watsonx", "custom"],
)
@pytest.mark.parametrize("blank", ["", "  ", "\r\n"])
def test_rerank_provider_refuses_a_blank_api_key(provider: str, blank: str) -> None:
    with pytest.raises(HarborRerankConfigurationError, match="empty api_key"):
        validate_rerank_configuration(_rerank_catalog(provider, blank))


@pytest.mark.parametrize("provider", ["litellm_proxy", "vllm", "infinity"])
def test_gateway_rerank_provider_accepts_a_set_api_key(provider: str) -> None:
    validate_rerank_configuration(_rerank_catalog(provider, "gateway-key"))
