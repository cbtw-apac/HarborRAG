"""Strict operator policy for deterministic and LLM graph products."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from harborrag_core.base import StrictModel

if TYPE_CHECKING:
    from harborrag_runtime.config.settings import RuntimeSettings


class _GraphBuildModel(StrictModel):
    """Strict scalar validation in addition to recursive immutability."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class GraphBuildBudgetConfig(_GraphBuildModel):
    """Durable per-tenant limits for extraction and summarization LLM calls."""

    max_concurrency: int = Field(default=4, ge=1, le=100)
    max_job_attempts: int = Field(default=10, ge=1, le=10)
    daily_token_cap: int = Field(default=2_000_000, ge=0)
    daily_cost_usd: Decimal = Field(default=Decimal("10"), ge=0)
    reservation_seconds: int = Field(default=300, ge=1, le=3600)

    @field_validator("daily_cost_usd", mode="before")
    @classmethod
    def parse_money(cls, value: object) -> Decimal:
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            raise ValueError("daily_cost_usd must be a decimal number")
        if isinstance(value, str) and (not value or value != value.strip()):
            raise ValueError("daily_cost_usd must not be blank or padded")
        try:
            parsed = Decimal(str(value))
        except InvalidOperation as error:
            raise ValueError("daily_cost_usd must be a decimal number") from error
        if not parsed.is_finite():
            raise ValueError("daily_cost_usd must be finite")
        return parsed


class GraphBuildExtractionConfig(_GraphBuildModel):
    """Safe overrides on the code-owned, deployment-pinned extraction profile."""

    max_input_chars: int = Field(default=12_000, ge=100, le=100_000)
    max_output_tokens: int = Field(default=2048, ge=128, le=16_384)
    max_windows: int = Field(default=8, ge=1, le=64)


class GraphBuildSourceConfig(_GraphBuildModel):
    source_scope_id: str = Field(min_length=1, max_length=128)
    enabled: bool = True
    model: str = Field(default="primary", min_length=1, max_length=256)
    extraction: GraphBuildExtractionConfig = Field(default_factory=GraphBuildExtractionConfig)


class GraphBuildTenantConfig(_GraphBuildModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    mode: Literal["deterministic", "llm"] = "deterministic"
    prohibited: bool = False
    spending_paused: bool = False
    budget: GraphBuildBudgetConfig = Field(default_factory=GraphBuildBudgetConfig)
    sources: list[GraphBuildSourceConfig] = Field(default_factory=list, max_length=10_000)

    @model_validator(mode="after")
    def validate_unique_sources(self) -> Self:
        source_ids = [source.source_scope_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError(f"tenant {self.tenant_id!r} has duplicate source_scope_id values")
        return self

    @property
    def llm_enabled(self) -> bool:
        """Resolved tenant switch; prohibition always wins."""

        return self.mode == "llm" and not self.prohibited


class GraphBuildDerivedConfig(_GraphBuildModel):
    """Optional independently retryable vector and parent-description products."""

    enabled: bool = False
    embedding_max_input_bytes: int = Field(default=8000, ge=100, le=100_000)
    parent_max_output_tokens: int = Field(default=1024, ge=128, le=4096)
    parent_max_fan_in: int = Field(default=8, ge=2, le=32)
    parent_max_input_bytes: int = Field(default=24000, ge=100, le=30000)
    parent_max_input_tokens: int = Field(default=6000, ge=100, le=30000)
    parent_max_calls: int = Field(default=64, ge=1, le=10000)


class GraphBuildRuntimeConfig(_GraphBuildModel):
    operation_timeout_seconds: float = Field(default=120, gt=0, le=3600)
    job_timeout_seconds: float = Field(default=3600, gt=0, le=86_400)
    lease_seconds: int = Field(default=300, ge=3, le=3600)
    max_chunks_per_document: int = Field(default=1000, ge=1, le=10_000)
    task_queue: str = Field(default="harborrag-topology", min_length=1, max_length=128)
    poll_seconds: float = Field(default=5, ge=1, le=60)
    llm_operation_cost_usd: Decimal | None = Field(default=None, gt=0)
    derived: GraphBuildDerivedConfig = Field(default_factory=GraphBuildDerivedConfig)

    @field_validator("llm_operation_cost_usd", mode="before")
    @classmethod
    def parse_optional_money(cls, value: object) -> Decimal | None:
        if value is None:
            return None
        return GraphBuildBudgetConfig.parse_money(value)


class GraphBuildConfig(_GraphBuildModel):
    """Desired graph-build policy; Postgres remains canonical runtime authority."""

    runtime: GraphBuildRuntimeConfig = Field(default_factory=GraphBuildRuntimeConfig)
    tenants: list[GraphBuildTenantConfig] = Field(default_factory=list, max_length=10_000)

    @model_validator(mode="after")
    def validate_unique_tenants(self) -> Self:
        tenant_ids = [tenant.tenant_id for tenant in self.tenants]
        if len(tenant_ids) != len(set(tenant_ids)):
            raise ValueError("graph-build configuration has duplicate tenant_id values")
        return self

    @classmethod
    def from_file(cls, path: str | Path) -> GraphBuildConfig:
        from harborrag_runtime.config.graph_build_loading import load_graph_build_config

        return load_graph_build_config(path)

    @classmethod
    def from_settings(cls, settings: RuntimeSettings) -> GraphBuildConfig:
        """Load YAML, retaining a safe no-LLM fallback outside the repository."""

        configured_path = Path(settings.graph_build_config_path).expanduser()
        if configured_path.is_file() or "graph_build_config_path" in settings.model_fields_set:
            return cls.from_file(configured_path)
        return cls()

    def effective_settings(self, settings: RuntimeSettings) -> RuntimeSettings:
        """Project the single YAML authority into internal runtime settings."""

        runtime = self.runtime
        derived = runtime.derived
        candidates: dict[str, object] = {
            "topology_operation_seconds": runtime.operation_timeout_seconds,
            "topology_job_seconds": runtime.job_timeout_seconds,
            "topology_lease_seconds": runtime.lease_seconds,
            "topology_max_chunks": runtime.max_chunks_per_document,
            "topology_task_queue": runtime.task_queue,
            "topology_poll_seconds": runtime.poll_seconds,
            "topology_embedding_max_input_bytes": derived.embedding_max_input_bytes,
            "topology_parent_max_output_tokens": derived.parent_max_output_tokens,
            "topology_parent_max_fan_in": derived.parent_max_fan_in,
            "topology_parent_max_input_bytes": derived.parent_max_input_bytes,
            "topology_parent_max_input_tokens": derived.parent_max_input_tokens,
            "topology_parent_max_calls": derived.parent_max_calls,
            "topology_llm_operation_cost_usd": runtime.llm_operation_cost_usd,
            "topology_derived_enabled": derived.enabled,
        }
        return settings.model_copy(update=candidates)


def graph_build_runtime_view(settings: RuntimeSettings) -> dict[str, object]:
    """Return the non-secret effective runtime subset used by graph building."""

    return {
        "operation_timeout_seconds": settings.topology_operation_seconds,
        "job_timeout_seconds": settings.topology_job_seconds,
        "lease_seconds": settings.topology_lease_seconds,
        "max_chunks_per_document": settings.topology_max_chunks,
        "task_queue": settings.topology_task_queue,
        "poll_seconds": settings.topology_poll_seconds,
        "derived_enabled": settings.topology_derived_enabled,
        "embedding_max_input_bytes": settings.topology_embedding_max_input_bytes,
        "parent_max_output_tokens": settings.topology_parent_max_output_tokens,
        "parent_max_fan_in": settings.topology_parent_max_fan_in,
        "parent_max_input_bytes": settings.topology_parent_max_input_bytes,
        "parent_max_input_tokens": settings.topology_parent_max_input_tokens,
        "parent_max_calls": settings.topology_parent_max_calls,
        "llm_operation_cost_usd": settings.topology_llm_operation_cost_usd,
    }
