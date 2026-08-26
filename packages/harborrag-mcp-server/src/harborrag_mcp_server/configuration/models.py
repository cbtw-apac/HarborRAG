"""Typed, fail-closed MCP tool configuration contracts."""

from __future__ import annotations

import json
import math
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PolicyConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_results: int = Field(default=20, ge=1, le=20)
    max_argument_bytes: int = Field(default=64 * 1024, ge=1, le=64 * 1024)
    max_output_bytes: int = Field(default=1024 * 1024, ge=1, le=1024 * 1024)
    allow_ingestion: bool = False


class ToolConfiguration(BaseModel):
    """Overrides for one globally or tenant-scoped registered tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool | None = None
    defaults: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, int | float] = Field(default_factory=dict)

    @field_validator("defaults")
    @classmethod
    def defaults_must_be_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("tool defaults must be finite JSON values") from exc
        return value

    @field_validator("limits")
    @classmethod
    def limits_must_be_positive(
        cls,
        value: dict[str, int | float],
    ) -> dict[str, int | float]:
        for field_name, limit in value.items():
            if isinstance(limit, bool) or not math.isfinite(limit) or limit <= 0:
                raise ValueError(f"tool limit {field_name!r} must be a positive number")
        return value


class TenantConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tools: dict[str, ToolConfiguration] = Field(default_factory=dict)


class McpConfiguration(BaseModel):
    """Versioned operator configuration persisted as YAML."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    policy: PolicyConfiguration = Field(default_factory=PolicyConfiguration)
    tools: dict[str, ToolConfiguration] = Field(default_factory=dict)
    tenants: dict[str, TenantConfiguration] = Field(default_factory=dict)

    @model_validator(mode="after")
    def identities_must_be_non_empty(self) -> Self:
        empty_tools = [name for name in self.tools if not name.strip()]
        empty_tenants = [name for name in self.tenants if not name.strip()]
        nested_empty_tools = [
            name for tenant in self.tenants.values() for name in tenant.tools if not name.strip()
        ]
        if empty_tools or nested_empty_tools:
            raise ValueError("MCP tool names must be non-empty")
        if empty_tenants:
            raise ValueError("MCP tenant names must be non-empty")
        noncanonical_tenants = [name for name in self.tenants if name != name.strip()]
        if noncanonical_tenants:
            raise ValueError("MCP tenant names must not contain surrounding whitespace")
        return self


__all__ = [
    "McpConfiguration",
    "PolicyConfiguration",
    "TenantConfiguration",
    "ToolConfiguration",
]
