from __future__ import annotations

from typing import Any, ClassVar, Self

from harborrag_core.models.capabilities import HarborEmbedCapabilities
from harborrag_core.models.embed import EmbeddingEncodingFormat, EmbeddingPurpose
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
from .registry import EmbedProviderRegistry, HarborEmbedProvider


class HarborEmbedSecurityConfig(SecurityBaseConfig):
    """Configure harbor embed security config behavior for HarborRAG model execution."""

    allowed_providers: frozenset[HarborEmbedProvider] | None = None
    allowed_extra_litellm_params: frozenset[str] = frozenset(
        {
            "input_type",
            "truncate",
            "task_type",
            "output_dimension",
            "output_dtype",
            "encoding_format",
            "drop_params",
            "additional_drop_params",
            "model_id",
            "role_name",
            "vertex_project",
            "vertex_location",
            "aws_bedrock_runtime_endpoint",
        }
    )


class HarborEmbedProviderConfig(ProviderDeploymentConfig):
    """One concrete embedding deployment behind a logical model."""

    provider: HarborEmbedProvider
    max_batch_size: int | None = Field(default=None, gt=0)
    expected_dimensions: int | None = Field(default=None, gt=0)
    capabilities: HarborEmbedCapabilities = Field(default_factory=HarborEmbedCapabilities)

    @model_validator(mode="after")
    def validate_provider_requirements(self) -> Self:
        """Enforce the provider's required fields, credential, and custom-provider rules."""
        descriptor = EmbedProviderRegistry.default().get(self.provider)
        return self.validate_provider_metadata(descriptor)


class HarborEmbedDefaults(BaseModel):
    """Configure harbor embed defaults behavior for HarborRAG model execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dimensions: int | None = Field(default=None, gt=0)
    encoding_format: EmbeddingEncodingFormat = EmbeddingEncodingFormat.FLOAT
    purpose: EmbeddingPurpose = EmbeddingPurpose.UNSPECIFIED
    normalize: bool = False
    batch_size: int | None = Field(default=None, gt=0)


class HarborEmbedModelConfig(LogicalModelConfig):
    """Configure harbor embed model config behavior for HarborRAG model execution."""

    embedding_space: str | None = Field(default=None, min_length=1)
    deployments: tuple[HarborEmbedProviderConfig, ...]
    default_params: HarborEmbedDefaults = Field(default_factory=HarborEmbedDefaults)

    @model_validator(mode="after")
    def ensure_deployments(self) -> Self:
        """Require at least one deployment with a unique name and consistent dimensions."""
        validate_unique_deployments(self.deployments, family_name="embedding")
        dimensions = {
            deployment.expected_dimensions
            for deployment in self.deployments
            if deployment.expected_dimensions is not None
        }
        if len(dimensions) > 1:
            raise ValueError(
                "deployments within one logical embedding model must use the same dimensions"
            )
        return self


class HarborEmbedClientConfig(ModelClientConfig):
    """Configure harbor embed client config behavior for HarborRAG model execution."""

    config_section: ClassVar[str] = "embed"

    default_batch_size: int = Field(default=128, gt=0, le=2048)
    max_inputs_per_request: int = Field(default=2048, gt=0, le=100_000)
    max_characters_per_input: int = Field(default=200_000, gt=0)
    security: HarborEmbedSecurityConfig = Field(default_factory=HarborEmbedSecurityConfig)
    models: dict[str, HarborEmbedModelConfig]

    @model_validator(mode="before")
    @classmethod
    def normalize_single_deployment_shorthand(cls, raw: Any) -> Any:
        """Expand a single-deployment model shorthand into the full ``deployments`` list form."""
        return normalize_single_deployment_shorthand(
            raw,
            provider_fields=frozenset(HarborEmbedProviderConfig.model_fields),
        )

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        """Validate aliases, fallback references, embedding-space, and dimension compatibility."""
        aliases = validate_logical_model_references(
            self.models,
            default_model=self.default_model,
            family_name="embedding",
        )
        for name, model in self.models.items():
            source_space = model.embedding_space or name
            source_dimensions = self._model_dimensions(model)
            for fallback in model.fallbacks:
                target_name = aliases.get(fallback, fallback)
                target = self.models[target_name]
                target_space = target.embedding_space or target_name
                if target_space != source_space:
                    raise ValueError(
                        f"embedding fallback {name!r} -> {target_name!r} crosses "
                        "incompatible embedding spaces; set the same explicit "
                        "embedding_space only when both models are index-compatible"
                    )
                target_dimensions = self._model_dimensions(target)
                if (
                    source_dimensions is not None
                    and target_dimensions is not None
                    and source_dimensions != target_dimensions
                ):
                    raise ValueError(
                        f"embedding fallback {name!r} -> {target_name!r} has "
                        "incompatible dimensions"
                    )
        if self.security.allowed_providers is not None:
            disallowed = {
                deployment.provider
                for model in self.models.values()
                for deployment in model.deployments
                if deployment.provider not in self.security.allowed_providers
            }
            if disallowed:
                raise ValueError(
                    "embedding providers are not allowed by security policy: "
                    + ", ".join(sorted(item.value for item in disallowed))
                )
        return self

    def resolve_alias(self, name: str) -> str | None:
        """Resolve a logical model name or alias to its canonical logical model name."""
        return resolve_logical_model(self.models, name)

    def model_for(self, name: str | None = None) -> tuple[str, HarborEmbedModelConfig]:
        """Resolve a logical model name (or the default) to its name and configuration."""
        requested = name or self.default_model
        resolved = self.resolve_alias(requested)
        if resolved is None:
            raise KeyError(f"unknown logical embedding model: {requested}")
        return resolved, self.models[resolved]

    @staticmethod
    def _model_dimensions(model: HarborEmbedModelConfig) -> int | None:
        """Return the shared expected dimensions across a model's deployments, if unambiguous."""
        dimensions = {
            deployment.expected_dimensions
            for deployment in model.deployments
            if deployment.expected_dimensions is not None
        }
        return next(iter(dimensions)) if len(dimensions) == 1 else None
