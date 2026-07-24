from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from types import MappingProxyType

from harborrag_adapters.models.runtime.provider import (
    ImmutableProviderRegistry,
    ProviderMetadata,
)


class HarborEmbedProvider(StrEnum):
    """Enumerate supported harbor embed provider values."""

    OPENAI = "openai"
    AZURE_OPENAI = "azure_openai"
    BEDROCK = "bedrock"
    COHERE = "cohere"
    GEMINI = "gemini"
    VERTEX_AI = "vertex_ai"
    VOYAGE = "voyage"
    HUGGINGFACE = "huggingface"
    OLLAMA = "ollama"
    OPENAI_COMPATIBLE = "openai_compatible"
    INFINITY = "infinity"
    VLLM = "vllm"
    LITELLM_PROXY = "litellm_proxy"
    CUSTOM = "custom"


EmbedProviderDescriptor = ProviderMetadata


_DEFAULT_DESCRIPTORS: Mapping[HarborEmbedProvider, EmbedProviderDescriptor] = MappingProxyType(
    {
        HarborEmbedProvider.OPENAI: EmbedProviderDescriptor(
            HarborEmbedProvider.OPENAI, "openai", requires_api_key=True
        ),
        HarborEmbedProvider.AZURE_OPENAI: EmbedProviderDescriptor(
            HarborEmbedProvider.AZURE_OPENAI,
            "azure",
            required_fields=frozenset({"api_base", "api_version", "deployment_name"}),
            requires_api_key=True,
        ),
        HarborEmbedProvider.BEDROCK: EmbedProviderDescriptor(
            HarborEmbedProvider.BEDROCK,
            "bedrock",
            required_fields=frozenset({"aws_region_name"}),
            supports_ambient_credentials=True,
            explicit_credential_sets=(frozenset({"aws_access_key_id", "aws_secret_access_key"}),),
        ),
        HarborEmbedProvider.COHERE: EmbedProviderDescriptor(
            HarborEmbedProvider.COHERE, "cohere", requires_api_key=True
        ),
        HarborEmbedProvider.GEMINI: EmbedProviderDescriptor(
            HarborEmbedProvider.GEMINI, "gemini", requires_api_key=True
        ),
        HarborEmbedProvider.VERTEX_AI: EmbedProviderDescriptor(
            HarborEmbedProvider.VERTEX_AI,
            "vertex_ai",
            supports_ambient_credentials=True,
            explicit_credential_sets=(frozenset({"vertex_credentials"}),),
        ),
        HarborEmbedProvider.VOYAGE: EmbedProviderDescriptor(
            HarborEmbedProvider.VOYAGE, "voyage", requires_api_key=True
        ),
        HarborEmbedProvider.HUGGINGFACE: EmbedProviderDescriptor(
            HarborEmbedProvider.HUGGINGFACE,
            "huggingface",
            requires_api_key=True,
        ),
        HarborEmbedProvider.OLLAMA: EmbedProviderDescriptor(
            HarborEmbedProvider.OLLAMA,
            "ollama",
            required_fields=frozenset({"api_base"}),
            requires_custom_base_url=True,
        ),
        HarborEmbedProvider.OPENAI_COMPATIBLE: EmbedProviderDescriptor(
            HarborEmbedProvider.OPENAI_COMPATIBLE,
            "openai",
            required_fields=frozenset({"api_base"}),
            requires_custom_base_url=True,
        ),
        HarborEmbedProvider.INFINITY: EmbedProviderDescriptor(
            HarborEmbedProvider.INFINITY,
            "infinity",
            required_fields=frozenset({"api_base"}),
            requires_custom_base_url=True,
        ),
        HarborEmbedProvider.VLLM: EmbedProviderDescriptor(
            HarborEmbedProvider.VLLM,
            "hosted_vllm",
            required_fields=frozenset({"api_base"}),
            requires_custom_base_url=True,
        ),
        HarborEmbedProvider.LITELLM_PROXY: EmbedProviderDescriptor(
            HarborEmbedProvider.LITELLM_PROXY,
            "openai",
            required_fields=frozenset({"api_base"}),
            requires_custom_base_url=True,
        ),
        HarborEmbedProvider.CUSTOM: EmbedProviderDescriptor(
            HarborEmbedProvider.CUSTOM,
            None,
            required_fields=frozenset({"custom_llm_provider"}),
        ),
    }
)


class EmbedProviderRegistry(
    ImmutableProviderRegistry[HarborEmbedProvider, EmbedProviderDescriptor]
):
    """Resolve typed embedding-provider metadata without global mutable state."""

    def __init__(
        self,
        descriptors: (
            Mapping[HarborEmbedProvider, EmbedProviderDescriptor]
            | Iterable[EmbedProviderDescriptor]
            | None
        ) = None,
    ) -> None:
        """Store an immutable copy of the given (or default) provider descriptors."""
        super().__init__(_DEFAULT_DESCRIPTORS if descriptors is None else descriptors)

    @classmethod
    def default(cls) -> EmbedProviderRegistry:
        """Build a registry seeded with HarborRAG's built-in provider descriptors."""
        return cls(_DEFAULT_DESCRIPTORS)
