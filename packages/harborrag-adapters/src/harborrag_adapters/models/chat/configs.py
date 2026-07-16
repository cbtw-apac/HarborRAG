from __future__ import annotations

from typing import Any, ClassVar, Self

from harborrag_core.models.capabilities import HarborChatCapabilities
from harborrag_core.models.chat import StructuredOutputDegradation
from pydantic import BaseModel, ConfigDict, Field, model_validator

from harborrag_adapters.models.common.config import ModelClientConfig, SecurityBaseConfig
from harborrag_adapters.models.common.model_config import (
    LogicalModelConfig,
    normalize_single_deployment_shorthand,
    resolve_logical_model,
    validate_logical_model_references,
    validate_unique_deployments,
)
from harborrag_adapters.models.common.provider import ProviderDeploymentConfig
from .registry import HarborProvider, ProviderRegistry


class HarborChatSecurityConfig(SecurityBaseConfig):
    """Restrict providers, endpoints, headers, and LiteLLM extension parameters."""

    allowed_providers: frozenset[HarborProvider] | None = None
    allowed_extra_litellm_params: frozenset[str] = frozenset(
        {
            "organization",
            "project",
            "service_tier",
            "reasoning_effort",
            "verbosity",
            "safety_identifier",
            "drop_params",
            "additional_drop_params",
            "enable_json_schema_validation",
            "model_id",
            "role_name",
        }
    )


class HarborChatProviderConfig(ProviderDeploymentConfig):
    """Describe one concrete provider deployment behind a logical model."""

    provider: HarborProvider
    capabilities: HarborChatCapabilities = Field(default_factory=HarborChatCapabilities)

    @model_validator(mode="after")
    def validate_provider_requirements(self) -> Self:
        """Validate provider metadata, credentials, and custom-provider settings."""

        descriptor = ProviderRegistry.default().get(self.provider)
        return self.validate_provider_metadata(descriptor)


class GenerationDefaults(BaseModel):
    """Store optional generation parameters inherited by chat requests."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, gt=0)
    max_completion_tokens: int | None = Field(default=None, gt=0)
    stop: str | list[str] | None = None
    seed: int | None = None


class StructuredOutputPolicyConfig(BaseModel):
    """Configure explicit structured-output degradation and bounded repair."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    degradation: StructuredOutputDegradation = StructuredOutputDegradation.JSON_MODE
    max_repair_attempts: int = Field(default=1, ge=0, le=3)


class HarborChatModelConfig(LogicalModelConfig):
    """Group compatible deployments, aliases, defaults, and model fallbacks."""

    deployments: tuple[HarborChatProviderConfig, ...]
    default_params: GenerationDefaults = Field(default_factory=GenerationDefaults)

    @model_validator(mode="after")
    def ensure_deployments(self) -> Self:
        """Require at least one uniquely named deployment."""

        validate_unique_deployments(self.deployments, family_name="chat")
        return self


class HarborChatClientConfig(ModelClientConfig):
    """Define the complete validated runtime configuration for HarborChat."""

    config_section: ClassVar[str] = "chat"

    security: HarborChatSecurityConfig = Field(default_factory=HarborChatSecurityConfig)
    structured_output: StructuredOutputPolicyConfig = Field(
        default_factory=StructuredOutputPolicyConfig
    )
    models: dict[str, HarborChatModelConfig]

    @model_validator(mode="before")
    @classmethod
    def normalize_single_deployment_shorthand(cls, raw: Any) -> Any:
        """Convert compact logical-model entries into deployment collections."""

        return normalize_single_deployment_shorthand(
            raw,
            provider_fields=frozenset(HarborChatProviderConfig.model_fields),
        )

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        """Reject missing defaults, duplicate aliases, and unknown fallbacks."""

        validate_logical_model_references(
            self.models,
            default_model=self.default_model,
            family_name="chat",
        )
        return self

    def resolve_alias(self, name: str) -> str | None:
        """Resolve a model name or alias to its canonical logical-model name."""

        return resolve_logical_model(self.models, name)

    def model_for(self, name: str | None = None) -> tuple[str, HarborChatModelConfig]:
        """Return the canonical name and configuration for a requested model."""

        requested = name or self.default_model
        resolved = self.resolve_alias(requested)
        if resolved is None:
            raise KeyError(f"unknown logical model: {requested}")
        return resolved, self.models[resolved]
