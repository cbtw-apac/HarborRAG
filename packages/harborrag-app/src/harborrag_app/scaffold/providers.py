"""Provider presets used to render config/models.yaml and the .env credentials block."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProviderPreset:
    key: str
    provider: str
    label: str
    model_prefix: str
    default_chat_model: str
    default_embed_model: str
    embed_dimensions: int | None
    credential_variable: str
    requires_api_base: bool
    api_base_variable: str | None
    api_version: str | None


PRESETS: dict[str, ProviderPreset] = {
    "openai": ProviderPreset(
        key="openai",
        provider="openai",
        label="OpenAI",
        model_prefix="openai/",
        default_chat_model="gpt-4o-mini",
        default_embed_model="text-embedding-3-small",
        embed_dimensions=1536,
        credential_variable="OPENAI_API_KEY",
        requires_api_base=False,
        api_base_variable=None,
        api_version=None,
    ),
    "azure-openai": ProviderPreset(
        key="azure-openai",
        provider="azure_openai",
        label="Azure OpenAI",
        model_prefix="azure/",
        default_chat_model="gpt-4o-mini",
        default_embed_model="text-embedding-3-small",
        embed_dimensions=1536,
        credential_variable="AZURE_OPENAI_API_KEY",
        requires_api_base=True,
        api_base_variable="AZURE_OPENAI_ENDPOINT",
        api_version="2024-10-21",
    ),
    "gemini": ProviderPreset(
        key="gemini",
        provider="gemini",
        label="Google Gemini",
        model_prefix="gemini/",
        default_chat_model="gemini-2.0-flash",
        default_embed_model="text-embedding-004",
        embed_dimensions=768,
        credential_variable="GEMINI_API_KEY",
        requires_api_base=False,
        api_base_variable=None,
        api_version=None,
    ),
    "openai-compatible": ProviderPreset(
        key="openai-compatible",
        provider="openai",
        label="OpenAI-compatible gateway",
        model_prefix="openai/",
        default_chat_model="gpt-4o-mini",
        default_embed_model="text-embedding-3-small",
        # Most gateways front OpenAI-shaped models; `init --embed-dimensions` overrides.
        embed_dimensions=1536,
        credential_variable="OPENAI_COMPATIBLE_API_KEY",
        requires_api_base=True,
        api_base_variable="OPENAI_COMPATIBLE_API_BASE",
        api_version=None,
    ),
}

DEFAULT_PROVIDER = "openai"

__all__ = ["DEFAULT_PROVIDER", "PRESETS", "ProviderPreset"]
