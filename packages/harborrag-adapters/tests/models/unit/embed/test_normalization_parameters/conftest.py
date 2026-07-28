from __future__ import annotations

from typing import Any

from pydantic import SecretStr

from harborrag_adapters.models.embed.configs import HarborEmbedProviderConfig
from harborrag_adapters.models.embed.registry import HarborEmbedProvider
from harborrag_core.models.capabilities import HarborEmbedCapabilities
from harborrag_core.models.embed import EmbeddingPurpose


def deployment(**updates: Any) -> HarborEmbedProviderConfig:
    values: dict[str, Any] = {
        "name": "embed-a",
        "provider": HarborEmbedProvider.OPENAI,
        "model": "openai/embed-test",
        "api_key": "secret",
        "headers": {"X-Deploy": SecretStr("one")},
        "expected_dimensions": 3,
        "max_batch_size": 4,
        "capabilities": HarborEmbedCapabilities(
            batch=True,
            max_batch_size=3,
            token_inputs=True,
            configurable_dimensions=True,
            default_dimensions=3,
            encoding_format=True,
            purpose=True,
            supported_purposes=frozenset(EmbeddingPurpose),
        ),
    }
    values.update(updates)
    return HarborEmbedProviderConfig(**values)


def raw_batch(*vectors: list[float], model: str = "provider-embed") -> dict[str, Any]:
    return {
        "model": model,
        "data": [{"index": index, "embedding": vector} for index, vector in enumerate(vectors)],
        "usage": {"prompt_tokens": len(vectors), "total_tokens": len(vectors) + 1},
        "_hidden_params": {
            "custom_llm_provider": "openai",
            "model_id": "embed-a",
            "request_id": "provider-request",
            "response_cost": 0.02,
            "cache_hit": True,
            "region_name": "us",
        },
    }
