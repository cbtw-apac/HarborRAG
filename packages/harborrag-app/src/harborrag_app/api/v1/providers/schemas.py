"""Strict public schemas for provider CRUD, test-connection, routing, and cost."""

from __future__ import annotations

from datetime import datetime
from typing import Self

from pydantic import Field, JsonValue, model_validator

from harborrag_app.api.schemas import ApiModel
from harborrag_core.domain.provider import Provider, ProviderFamily
from harborrag_core.domain.routing_rule import RoutingRule
from harborrag_core.ports.provider_probe import ProviderProbeResult


class ProviderCreateInput(ApiModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    family: ProviderFamily
    config: dict[str, JsonValue] = Field(default_factory=dict)
    api_key: str | None = Field(default=None, min_length=1)


class ProviderUpdateInput(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    config: dict[str, JsonValue] | None = None
    api_key: str | None = None

    @model_validator(mode="after")
    def reject_explicit_null_config(self) -> Self:
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name must be omitted, not null")
        """`{"config": null}` must 422, not crash the secret-free validation path."""
        if "config" in self.model_fields_set and self.config is None:
            raise ValueError("config must be omitted, not null; send {} to clear it")
        return self


class ProviderOut(ApiModel):
    id: str
    tenant_id: str
    name: str
    family: ProviderFamily
    config: dict[str, JsonValue]
    secret_ref: str | None

    @classmethod
    def from_domain(cls, provider: Provider) -> ProviderOut:
        return cls(
            id=provider.id,
            tenant_id=provider.tenant_id,
            name=provider.name,
            family=provider.family,
            config=dict(provider.config),
            secret_ref=provider.secret_ref,
        )


class ProviderTestResult(ApiModel):
    ok: bool
    message: str
    latency_ms: float = Field(ge=0)

    @classmethod
    def from_domain(cls, result: ProviderProbeResult) -> ProviderTestResult:
        return cls(ok=result.ok, message=result.message, latency_ms=result.latency_ms)


class RoutingRuleInput(ApiModel):
    family: ProviderFamily
    provider_id: str = Field(min_length=1, max_length=255)
    priority: int = Field(default=0, ge=0, le=1000)


class RoutingRuleOut(ApiModel):
    id: str
    family: ProviderFamily
    provider_id: str
    priority: int

    @classmethod
    def from_domain(cls, rule: RoutingRule) -> RoutingRuleOut:
        return cls(
            id=rule.id, family=rule.family, provider_id=rule.provider_id, priority=rule.priority
        )


class ProviderCostResponse(ApiModel):
    """A live spend snapshot, not permanent history -- see ``since``."""

    since: datetime = Field(description="Process start time; counters reset on every app restart.")
    providers: dict[str, float] = Field(description="Total spend in USD per provider id.")
    note: str = "In-memory snapshot since the last app restart; not permanent history."
