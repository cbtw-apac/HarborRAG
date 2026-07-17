from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from types import MappingProxyType

from harborrag_adapters.models.common.provider import (
    ImmutableProviderRegistry,
    ProviderMetadata,
)


class HarborProvider(StrEnum):
    """Enumerate supported harbor provider values."""

    OPENAI = "openai"
    AZURE_OPENAI = "azure_openai"
    BEDROCK = "bedrock"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    OLLAMA = "ollama"
    OPENAI_COMPATIBLE = "openai_compatible"
    VLLM = "vllm"
    LITELLM_PROXY = "litellm_proxy"
    CUSTOM = "custom"


ProviderDescriptor = ProviderMetadata


_DEFAULT_DESCRIPTORS: Mapping[HarborProvider, ProviderDescriptor] = MappingProxyType(
    {
        HarborProvider.OPENAI: ProviderDescriptor(
            name=HarborProvider.OPENAI, litellm_provider="openai", requires_api_key=True
        ),
        HarborProvider.AZURE_OPENAI: ProviderDescriptor(
            name=HarborProvider.AZURE_OPENAI,
            litellm_provider="azure",
            required_fields=frozenset({"api_base", "api_version", "deployment_name"}),
            requires_api_key=True,
        ),
        HarborProvider.BEDROCK: ProviderDescriptor(
            name=HarborProvider.BEDROCK,
            litellm_provider="bedrock",
            required_fields=frozenset({"aws_region_name"}),
            supports_ambient_credentials=True,
            explicit_credential_sets=(frozenset({"aws_access_key_id", "aws_secret_access_key"}),),
        ),
        HarborProvider.ANTHROPIC: ProviderDescriptor(
            name=HarborProvider.ANTHROPIC, litellm_provider="anthropic", requires_api_key=True
        ),
        HarborProvider.GEMINI: ProviderDescriptor(
            name=HarborProvider.GEMINI, litellm_provider="gemini", requires_api_key=True
        ),
        HarborProvider.OLLAMA: ProviderDescriptor(
            name=HarborProvider.OLLAMA,
            litellm_provider="ollama",
            required_fields=frozenset({"api_base"}),
            requires_custom_base_url=True,
        ),
        HarborProvider.OPENAI_COMPATIBLE: ProviderDescriptor(
            name=HarborProvider.OPENAI_COMPATIBLE,
            litellm_provider="openai",
            required_fields=frozenset({"api_base"}),
            requires_custom_base_url=True,
        ),
        HarborProvider.VLLM: ProviderDescriptor(
            name=HarborProvider.VLLM,
            litellm_provider="hosted_vllm",
            required_fields=frozenset({"api_base"}),
            requires_custom_base_url=True,
        ),
        HarborProvider.LITELLM_PROXY: ProviderDescriptor(
            name=HarborProvider.LITELLM_PROXY,
            litellm_provider="litellm_proxy",
        ),
        HarborProvider.CUSTOM: ProviderDescriptor(
            name=HarborProvider.CUSTOM,
            litellm_provider=None,
            required_fields=frozenset({"custom_llm_provider"}),
        ),
    }
)


class ProviderRegistry(ImmutableProviderRegistry[HarborProvider, ProviderDescriptor]):
    """Read-only provider registry; no import-time mutable singleton is created."""

    def __init__(
        self,
        descriptors: (
            Mapping[HarborProvider, ProviderDescriptor] | Iterable[ProviderDescriptor] | None
        ) = None,
    ) -> None:
        """Store an immutable copy of the given (or default) provider descriptors."""
        super().__init__(_DEFAULT_DESCRIPTORS if descriptors is None else descriptors)

    @classmethod
    def default(cls) -> ProviderRegistry:
        """Build a registry seeded with HarborRAG's built-in provider descriptors."""
        return cls(_DEFAULT_DESCRIPTORS)
