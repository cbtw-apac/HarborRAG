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


@pytest.mark.parametrize("provider", ["litellm_proxy", "vllm", "infinity"])
@pytest.mark.parametrize("blank", ["", "  ", "\r\n"])
def test_rerank_provider_that_needs_no_key_accepts_a_blank_one(provider: str, blank: str) -> None:
    validate_rerank_configuration(_rerank_catalog(provider, blank))


@pytest.mark.parametrize("provider", ["cohere", "jina_ai", "voyage", "watsonx", "custom"])
def test_rerank_provider_that_needs_or_may_borrow_a_key_refuses_a_blank_one(provider: str) -> None:
    with pytest.raises(HarborRerankConfigurationError, match="empty api_key"):
        validate_rerank_configuration(_rerank_catalog(provider, ""))
