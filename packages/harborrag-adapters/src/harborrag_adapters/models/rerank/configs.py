from __future__ import annotations

from typing import Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from harborrag_adapters.models.runtime.config import (
    ModelClientConfig,
    SecurityBaseConfig,
)
from harborrag_adapters.models.runtime.model_config import (
    LogicalModelConfig,
    normalize_single_deployment_shorthand,
    resolve_logical_model,
    validate_capability_compatibility,
    validate_logical_model_references,
    validate_unique_deployments,
)
from harborrag_adapters.models.runtime.provider import ProviderDeploymentConfig
from harborrag_core.models.capabilities import HarborRerankCapabilities

from .registry import HarborRerankProvider


class HarborRerankSecurityConfig(SecurityBaseConfig):
    """Configure harbor rerank security config behavior for HarborRAG model execution."""

    allowed_providers: frozenset[HarborRerankProvider] | None = None
    allowed_extra_litellm_params: frozenset[str] = frozenset(
        {
            "truncate",
            "priority",
            "drop_params",
            "additional_drop_params",
            "model_id",
            "role_name",
            "aws_bedrock_runtime_endpoint",
            "api_type",
        }
    )


class HarborRerankProviderConfig(ProviderDeploymentConfig):
    """One concrete reranking deployment behind a logical model."""

    provider: HarborRerankProvider
    max_documents: int | None = Field(default=None, gt=0)
    capabilities: HarborRerankCapabilities = Field(default_factory=HarborRerankCapabilities)


class HarborRerankDefaults(BaseModel):
    """Configure harbor rerank defaults behavior for HarborRAG model execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    top_n: int | None = Field(default=None, gt=0)
    return_documents: bool = True
    max_chunks_per_doc: int | None = Field(default=None, gt=0)
    max_tokens_per_doc: int | None = Field(default=None, gt=0)
    instruction: str | None = None


class HarborRerankModelConfig(LogicalModelConfig):
    """Configure harbor rerank model config behavior for HarborRAG model execution."""

    deployments: tuple[HarborRerankProviderConfig, ...]
    default_params: HarborRerankDefaults = Field(default_factory=HarborRerankDefaults)

    @model_validator(mode="after")
    def ensure_deployments(self) -> Self:
        """Require at least one deployment with a unique name."""
        validate_unique_deployments(self.deployments, family_name="reranking")
        return self


class HarborRerankClientConfig(ModelClientConfig):
    """Configure harbor rerank client config behavior for HarborRAG model execution."""

    config_section: ClassVar[str] = "rerank"

    max_documents_per_request: int = Field(default=1_000, gt=0, le=100_000)
    max_query_characters: int = Field(default=100_000, gt=0)
    max_document_characters: int = Field(default=500_000, gt=0)
    security: HarborRerankSecurityConfig = Field(default_factory=HarborRerankSecurityConfig)
    models: dict[str, HarborRerankModelConfig]

    @model_validator(mode="before")
    @classmethod
    def normalize_single_deployment_shorthand(cls, raw: Any) -> Any:
        """Expand a single-deployment model shorthand into the full ``deployments`` list form."""
        return normalize_single_deployment_shorthand(
            raw,
            provider_fields=frozenset(HarborRerankProviderConfig.model_fields),
        )

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        """Validate aliases, fallback references, and provider security policy."""
        validate_logical_model_references(
            self.models,
            default_model=self.default_model,
            family_name="rerank",
        )
        validate_capability_compatibility(self.models, family_name="rerank")
        return self

    def resolve_alias(self, name: str) -> str | None:
        """Resolve a logical model name or alias to its canonical logical model name."""
        return resolve_logical_model(self.models, name)

    def model_for(self, name: str | None = None) -> tuple[str, HarborRerankModelConfig]:
        """Resolve a logical model name (or the default) to its name and configuration."""
        requested = name or self.default_model
        resolved = self.resolve_alias(requested)
        if resolved is None:
            raise KeyError(f"unknown logical reranking model: {requested}")
        return resolved, self.models[resolved]
