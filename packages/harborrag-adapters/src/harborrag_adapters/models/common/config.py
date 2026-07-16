from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .environment import expand_environment
from .loading import load_config_document, prepare_config_section
from .security import (
    PrivacyConfig,
    SecretResolver,
    resolve_secret_references,
    sanitize_configuration,
)


class RoutingEngine(StrEnum):
    """Enumerate supported routing engines."""

    HARBOR = "harbor"
    LITELLM_ROUTER = "litellm_router"


class RoutingStrategy(StrEnum):
    """Enumerate supported deployment-selection strategies."""

    ORDERED = "ordered"
    WEIGHTED = "weighted"
    ROUND_ROBIN = "round_robin"
    LEAST_BUSY = "least_busy"
    LATENCY = "latency"


class TelemetryFailureMode(StrEnum):
    """Control whether telemetry failures affect model operations."""

    IGNORE = "ignore"
    RAISE = "raise"


class CacheBackend(StrEnum):
    """Enumerate supported cache ownership boundaries."""

    CUSTOM = "custom"
    LITELLM = "litellm"


class TimeoutConfig(BaseModel):
    """Configure request and optional stream time limits in seconds."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_seconds: float = Field(default=60.0, gt=0)
    stream_seconds: float | None = Field(default=None, gt=0)


class RetryPolicyConfig(BaseModel):
    """Configure bounded retries, deployment failover, and model fallback."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    same_deployment_attempts: int = Field(default=2, ge=1, le=10)
    max_deployment_failovers: int = Field(default=2, ge=0, le=20)
    max_model_fallbacks: int = Field(default=2, ge=0, le=20)
    base_delay_seconds: float = Field(default=0.25, ge=0, le=60)
    max_delay_seconds: float = Field(default=8.0, ge=0, le=300)
    jitter_ratio: float = Field(default=0.2, ge=0, le=1)

    @model_validator(mode="after")
    def validate_delay_range(self) -> Self:
        """Reject a maximum delay smaller than the initial retry delay."""

        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError(
                "max_delay_seconds must be greater than or equal to base_delay_seconds"
            )
        return self


class CircuitBreakerConfig(BaseModel):
    """Configure per-deployment circuit breaking."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    failure_threshold: int = Field(default=5, ge=1)
    recovery_timeout_seconds: float = Field(default=30.0, ge=0.1)


class RoutingConfig(BaseModel):
    """Configure model routing and health tracking."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    engine: RoutingEngine = RoutingEngine.HARBOR
    strategy: RoutingStrategy = RoutingStrategy.WEIGHTED
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    enable_health_tracking: bool = True


class CapabilityPolicyConfig(BaseModel):
    """Control optional provider metadata discovery for capability resolution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allow_provider_metadata_discovery: bool = True


class ObservabilityConfig(BaseModel):
    """Configure telemetry dispatch and privacy behavior."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    failure_mode: TelemetryFailureMode = TelemetryFailureMode.IGNORE
    include_stream_events: bool = True
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)


class CacheConfig(BaseModel):
    """Configure safe response caching and tenant isolation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    backend: CacheBackend = CacheBackend.CUSTOM
    ttl_seconds: int = Field(default=300, ge=1)
    cache_sensitive_requests: bool = False
    require_tenant_id: bool = True
    key_namespace: str = Field(default="harborrag:model", min_length=1)
    max_entries: int = Field(default=1_024, ge=1, le=1_000_000)


class BudgetLimitConfig(BaseModel):
    """Configure an optional LiteLLM provider spend or rate budget."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_budget: float | None = Field(default=None, gt=0)
    budget_duration: str | None = Field(default=None, pattern=r"^[1-9]\d*[smhd]$")
    tpm_limit: int | None = Field(default=None, gt=0)
    rpm_limit: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_spend_window(self) -> Self:
        """Require spend amount and duration to be configured together."""

        if (self.max_budget is None) != (self.budget_duration is None):
            raise ValueError("max_budget and budget_duration must be configured together")
        return self


class SecurityBaseConfig(BaseModel):
    """Define transport and extension boundaries shared by model families."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed_base_url_hosts: frozenset[str] | None = None
    require_https_for_remote_endpoints: bool = True
    allow_custom_providers: bool = False
    allow_request_auth_headers: bool = False
    max_extra_params: int = Field(default=32, ge=0, le=256)


class ModelClientConfig(BaseModel):
    """Provide validated settings and loading behavior shared by every model family."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    config_section: ClassVar[str]

    default_model: str = Field(min_length=1)
    timeouts: TimeoutConfig = Field(default_factory=TimeoutConfig)
    retry: RetryPolicyConfig = Field(default_factory=RetryPolicyConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    provider_budgets: dict[str, BudgetLimitConfig] = Field(default_factory=dict)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    capability_policy: CapabilityPolicyConfig = Field(default_factory=CapabilityPolicyConfig)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_timeout_fields(cls, raw: Any) -> Any:
        """Accept legacy flat timeout fields while keeping one typed timeout schema."""

        if not isinstance(raw, Mapping):
            return raw
        data = dict(raw)
        legacy_fields = {
            "timeout_seconds": "request_seconds",
            "stream_timeout_seconds": "stream_seconds",
        }
        supplied_legacy = [name for name in legacy_fields if name in data]
        if "timeouts" in data and supplied_legacy:
            fields = ", ".join(supplied_legacy)
            raise ValueError(f"timeouts cannot be combined with legacy fields: {fields}")
        if supplied_legacy:
            data["timeouts"] = {
                target: data.pop(source)
                for source, target in legacy_fields.items()
                if source in data
            }
        return data

    @property
    def timeout_seconds(self) -> float:
        """Expose the request timeout for existing runtime consumers."""

        return self.timeouts.request_seconds

    @property
    def stream_timeout_seconds(self) -> float | None:
        """Expose the optional stream timeout for chat runtime consumers."""

        return self.timeouts.stream_seconds

    def sanitized_dump(self) -> dict[str, Any]:
        """Return a serialization-safe view with secrets and sensitive fields redacted."""

        return sanitize_configuration(self)

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        *,
        profile: str | None = None,
        overrides: Mapping[str, Any] | None = None,
        secret_resolver: SecretResolver | None = None,
    ) -> Self:
        """Build typed configuration from an in-memory mapping."""

        prepared = prepare_config_section(
            data,
            section=cls.config_section,
            profile=profile,
            overrides=overrides,
        )
        expanded = expand_environment(prepared)
        resolved = resolve_secret_references(expanded, secret_resolver)
        return cls.model_validate(resolved)

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        *,
        profile: str | None = None,
        overrides: Mapping[str, Any] | None = None,
        secret_resolver: SecretResolver | None = None,
    ) -> Self:
        """Build typed configuration from a YAML or JSON document."""

        return cls.from_dict(
            load_config_document(path),
            profile=profile,
            overrides=overrides,
            secret_resolver=secret_resolver,
        )
