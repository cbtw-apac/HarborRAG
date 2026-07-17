from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RoutingStateBackend(StrEnum):
    """Select process-local or Redis-backed routing state."""

    MEMORY = "memory"
    REDIS = "redis"


class SingleFlightBackend(StrEnum):
    """Select disabled, local, or Redis-coordinated request deduplication."""

    DISABLED = "disabled"
    MEMORY = "memory"
    REDIS = "redis"


class SingleFlightConfig(BaseModel):
    """Configure request deduplication for identical eligible cache keys."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: SingleFlightBackend = SingleFlightBackend.DISABLED
    lock_ttl_seconds: int = Field(default=120, ge=1, le=3_600)
    follower_timeout_seconds: float = Field(default=125.0, gt=0, le=3_700)
    poll_interval_seconds: float = Field(default=0.05, gt=0, le=5)

    @model_validator(mode="after")
    def validate_timeouts(self) -> SingleFlightConfig:
        """Require follower waiting to outlive the leader lock lease."""

        if self.follower_timeout_seconds <= self.lock_ttl_seconds:
            raise ValueError("singleflight follower timeout must exceed lock TTL")
        return self


class ActiveHealthConfig(BaseModel):
    """Configure optional background deployment health probes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    interval_seconds: float = Field(default=30.0, gt=0, le=3_600)
    timeout_seconds: float = Field(default=5.0, gt=0, le=300)
    stale_after_seconds: float = Field(default=90.0, gt=0, le=86_400)
    start_automatically: bool = False


class BudgetPolicyConfig(BaseModel):
    """Configure portable request, rate, and spend limits before provider calls."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    max_request_tokens: int | None = Field(default=None, gt=0)
    max_request_cost_usd: float | None = Field(default=None, gt=0)
    daily_cost_usd: float | None = Field(default=None, gt=0)
    monthly_cost_usd: float | None = Field(default=None, gt=0)
    rpm_limit: int | None = Field(default=None, gt=0)
    tpm_limit: int | None = Field(default=None, gt=0)
    require_tenant_id: bool = True
