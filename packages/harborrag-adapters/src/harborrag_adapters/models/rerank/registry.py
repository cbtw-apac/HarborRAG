from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from types import MappingProxyType

from harborrag_core.models.common.provider import ImmutableProviderRegistry, ProviderMetadata


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


RerankProviderDescriptor = ProviderMetadata


_DEFAULT_DESCRIPTORS: Mapping[HarborRerankProvider, RerankProviderDescriptor] = MappingProxyType(
    {
        HarborRerankProvider.COHERE: RerankProviderDescriptor(
            HarborRerankProvider.COHERE, "cohere", requires_api_key=True
        ),
        HarborRerankProvider.BEDROCK: RerankProviderDescriptor(
            HarborRerankProvider.BEDROCK,
            "bedrock",
            required_fields=frozenset({"aws_region_name"}),
            supports_ambient_credentials=True,
        ),
        HarborRerankProvider.TOGETHER_AI: RerankProviderDescriptor(
            HarborRerankProvider.TOGETHER_AI, "together_ai", requires_api_key=True
        ),
        HarborRerankProvider.AZURE_AI: RerankProviderDescriptor(
            HarborRerankProvider.AZURE_AI,
            "azure_ai",
            required_fields=frozenset({"api_base"}),
            requires_api_key=True,
        ),
        HarborRerankProvider.JINA_AI: RerankProviderDescriptor(
            HarborRerankProvider.JINA_AI, "jina_ai", requires_api_key=True
        ),
        HarborRerankProvider.HUGGINGFACE: RerankProviderDescriptor(
            HarborRerankProvider.HUGGINGFACE, "huggingface", requires_api_key=True
        ),
        HarborRerankProvider.INFINITY: RerankProviderDescriptor(
            HarborRerankProvider.INFINITY,
            "infinity",
            required_fields=frozenset({"api_base"}),
            requires_custom_base_url=True,
        ),
        HarborRerankProvider.VLLM: RerankProviderDescriptor(
            HarborRerankProvider.VLLM,
            "hosted_vllm",
            required_fields=frozenset({"api_base"}),
            requires_custom_base_url=True,
        ),
        HarborRerankProvider.NVIDIA_NIM: RerankProviderDescriptor(
            HarborRerankProvider.NVIDIA_NIM,
            "nvidia_nim",
            required_fields=frozenset({"api_base"}),
            requires_api_key=True,
        ),
        HarborRerankProvider.DEEPINFRA: RerankProviderDescriptor(
            HarborRerankProvider.DEEPINFRA, "deepinfra", requires_api_key=True
        ),
        HarborRerankProvider.VERTEX_AI: RerankProviderDescriptor(
            HarborRerankProvider.VERTEX_AI,
            "vertex_ai",
            supports_ambient_credentials=True,
        ),
        HarborRerankProvider.FIREWORKS_AI: RerankProviderDescriptor(
            HarborRerankProvider.FIREWORKS_AI, "fireworks_ai", requires_api_key=True
        ),
        HarborRerankProvider.VOYAGE: RerankProviderDescriptor(
            HarborRerankProvider.VOYAGE, "voyage", requires_api_key=True
        ),
        HarborRerankProvider.WATSONX: RerankProviderDescriptor(
            HarborRerankProvider.WATSONX,
            "watsonx",
            supports_ambient_credentials=True,
        ),
        HarborRerankProvider.LITELLM_PROXY: RerankProviderDescriptor(
            HarborRerankProvider.LITELLM_PROXY,
            "litellm_proxy",
            required_fields=frozenset({"api_base"}),
            requires_custom_base_url=True,
        ),
        HarborRerankProvider.CUSTOM: RerankProviderDescriptor(
            HarborRerankProvider.CUSTOM,
            None,
            required_fields=frozenset({"custom_llm_provider"}),
        ),
    }
)


class RerankProviderRegistry(
    ImmutableProviderRegistry[HarborRerankProvider, RerankProviderDescriptor]
):
    """Resolve typed reranking-provider metadata without global mutable state."""

    def __init__(
        self,
        descriptors: (
            Mapping[HarborRerankProvider, RerankProviderDescriptor]
            | Iterable[RerankProviderDescriptor]
            | None
        ) = None,
    ) -> None:
        """Store an immutable copy of the given (or default) provider descriptors."""
        super().__init__(_DEFAULT_DESCRIPTORS if descriptors is None else descriptors)

    @classmethod
    def default(cls) -> RerankProviderRegistry:
        """Build a registry seeded with HarborRAG's built-in provider descriptors."""
        return cls(_DEFAULT_DESCRIPTORS)
