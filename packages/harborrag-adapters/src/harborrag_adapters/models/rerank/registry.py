from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from types import MappingProxyType

from harborrag_adapters.models.runtime.provider import (
    ImmutableProviderRegistry,
    ProviderMetadata,
)


class HarborRerankProvider(StrEnum):
    """Enumerate supported harbor rerank provider values."""

    COHERE = "cohere"
    BEDROCK = "bedrock"
    TOGETHER_AI = "together_ai"
    AZURE_AI = "azure_ai"
    JINA_AI = "jina_ai"
    HUGGINGFACE = "huggingface"
    INFINITY = "infinity"
    VLLM = "vllm"
    NVIDIA_NIM = "nvidia_nim"
    DEEPINFRA = "deepinfra"
    VERTEX_AI = "vertex_ai"
    FIREWORKS_AI = "fireworks_ai"
    VOYAGE = "voyage"
    WATSONX = "watsonx"
    LITELLM_PROXY = "litellm_proxy"
    CUSTOM = "custom"


_DEFAULT_DESCRIPTORS: Mapping[HarborRerankProvider, ProviderMetadata] = MappingProxyType(
    {
        HarborRerankProvider.COHERE: ProviderMetadata(
            HarborRerankProvider.COHERE, "cohere", requires_api_key=True
        ),
        HarborRerankProvider.BEDROCK: ProviderMetadata(
            HarborRerankProvider.BEDROCK,
            "bedrock",
            required_fields=frozenset({"aws_region_name"}),
            supports_ambient_credentials=True,
            explicit_credential_sets=(frozenset({"aws_access_key_id", "aws_secret_access_key"}),),
        ),
        HarborRerankProvider.TOGETHER_AI: ProviderMetadata(
            HarborRerankProvider.TOGETHER_AI, "together_ai", requires_api_key=True
        ),
        HarborRerankProvider.AZURE_AI: ProviderMetadata(
            HarborRerankProvider.AZURE_AI,
            "azure_ai",
            required_fields=frozenset({"api_base"}),
            requires_api_key=True,
        ),
        HarborRerankProvider.JINA_AI: ProviderMetadata(
            HarborRerankProvider.JINA_AI, "jina_ai", requires_api_key=True
        ),
        HarborRerankProvider.HUGGINGFACE: ProviderMetadata(
            HarborRerankProvider.HUGGINGFACE, "huggingface", requires_api_key=True
        ),
        HarborRerankProvider.INFINITY: ProviderMetadata(
            HarborRerankProvider.INFINITY,
            "infinity",
            required_fields=frozenset({"api_base"}),
            requires_custom_base_url=True,
        ),
        HarborRerankProvider.VLLM: ProviderMetadata(
            HarborRerankProvider.VLLM,
            "hosted_vllm",
            required_fields=frozenset({"api_base"}),
            requires_custom_base_url=True,
        ),
        HarborRerankProvider.NVIDIA_NIM: ProviderMetadata(
            HarborRerankProvider.NVIDIA_NIM,
            "nvidia_nim",
            required_fields=frozenset({"api_base"}),
            requires_api_key=True,
        ),
        HarborRerankProvider.DEEPINFRA: ProviderMetadata(
            HarborRerankProvider.DEEPINFRA, "deepinfra", requires_api_key=True
        ),
        HarborRerankProvider.VERTEX_AI: ProviderMetadata(
            HarborRerankProvider.VERTEX_AI,
            "vertex_ai",
            supports_ambient_credentials=True,
            explicit_credential_sets=(frozenset({"vertex_credentials"}),),
        ),
        HarborRerankProvider.FIREWORKS_AI: ProviderMetadata(
            HarborRerankProvider.FIREWORKS_AI, "fireworks_ai", requires_api_key=True
        ),
        HarborRerankProvider.VOYAGE: ProviderMetadata(
            HarborRerankProvider.VOYAGE, "voyage", requires_api_key=True
        ),
        HarborRerankProvider.WATSONX: ProviderMetadata(
            HarborRerankProvider.WATSONX,
            "watsonx",
            supports_ambient_credentials=True,
            explicit_credential_sets=(frozenset({"api_key"}),),
        ),
        HarborRerankProvider.LITELLM_PROXY: ProviderMetadata(
            HarborRerankProvider.LITELLM_PROXY,
            "litellm_proxy",
            required_fields=frozenset({"api_base"}),
            requires_custom_base_url=True,
        ),
        HarborRerankProvider.CUSTOM: ProviderMetadata(
            HarborRerankProvider.CUSTOM,
            None,
            required_fields=frozenset({"custom_llm_provider"}),
        ),
    }
)


class RerankProviderRegistry(ImmutableProviderRegistry[HarborRerankProvider, ProviderMetadata]):
    """Resolve typed reranking-provider metadata without global mutable state."""

    def __init__(
        self,
        descriptors: (
            Mapping[HarborRerankProvider, ProviderMetadata] | Iterable[ProviderMetadata] | None
        ) = None,
    ) -> None:
        """Store an immutable copy of the given (or default) provider descriptors."""
        super().__init__(_DEFAULT_DESCRIPTORS if descriptors is None else descriptors)

    @classmethod
    def default(cls) -> RerankProviderRegistry:
        """Build a registry seeded with HarborRAG's built-in provider descriptors."""
        return cls(_DEFAULT_DESCRIPTORS)
