"""API process settings, loaded from HARBORRAG_* environment variables (ST2).

Static process config only (ports, auth mode, CORS); resource configuration
(projects/sources/providers) is DB-backed and API-mutable per plan §4.3.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from harborrag_core.security import RemoteTransportPolicy

from .capacity_scope import CapacityLimits, CapacityTierLimits

AuthMode = Literal["none", "hmac", "oidc"]
_CAPACITY_REDIS_TRANSPORT = RemoteTransportPolicy(
    service="API capacity Redis",
    allowed_schemes=frozenset({"redis", "rediss"}),
    secure_schemes=frozenset({"rediss"}),
)


class TenantCapacityOverride(BaseModel):
    """One tenant's admission-control limits; unset fields keep the default.

    ``extra="forbid"`` makes a typo in an override key a startup failure whose
    pydantic ``loc`` names the offending tenant, rather than a silently
    ignored limit that leaves that customer on the global default.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    requests_per_minute: int | None = Field(default=None, ge=1, le=10_000)
    max_inflight: int | None = Field(default=None, ge=1, le=100)
    user_requests_per_minute: int | None = Field(default=None, ge=1, le=10_000)
    user_max_inflight: int | None = Field(default=None, ge=1, le=100)
    tenant_requests_per_minute: int | None = Field(default=None, ge=1, le=100_000)
    tenant_max_inflight: int | None = Field(default=None, ge=1, le=1_000)


class ApiSettings(BaseSettings):
    """Environment-driven settings for the Control Plane API process."""

    model_config = SettingsConfigDict(env_prefix="HARBORRAG_", extra="ignore")

    # Safe local default. Container deployments explicitly override this to
    # 0.0.0.0 and must acknowledge disabled authentication in development.
    host: str = "127.0.0.1"
    port: int = 8000
    env: Literal["dev", "prod"] = "dev"
    cors_origins: list[str] = []
    auth_mode: AuthMode = "none"
    allow_insecure_dev: bool = False
    auth_secret: SecretStr | None = None
    auth_issuer: str = "harborrag"
    auth_audience: str = "harborrag-api"
    auth_max_token_lifetime_seconds: int = Field(default=3600, ge=60, le=86_400)
    auth_clock_skew_seconds: int = Field(default=30, ge=0, le=300)
    # JWT claim carrying the stable end-user identity used for user-scoped
    # memory; falls back to ``sub`` when the claim is absent from a token.
    auth_user_id_claim: str = Field(default="sub", min_length=1)
    max_request_body_bytes: int = Field(default=1_048_576, ge=1_024, le=16_777_216)
    api_capacity_redis_url: SecretStr | None = None
    api_capacity_allow_insecure_remote: bool = False
    # Admission control keys on three tiers. The per-principal pair below is
    # the historical default; the per-user pair matches it so a deployment
    # where one credential fronts one human is unchanged, while a shared
    # service credential now gives every human their own 60/4 instead of one
    # bucket for all of them. The tenant aggregate is 10x the per-user share:
    # a customer can run ~10 users at their full rate before the aggregate
    # binds, and no single customer can drain a deployment sized for several.
    api_requests_per_minute: int = Field(default=60, ge=1, le=10_000)
    api_max_inflight_per_principal: int = Field(default=4, ge=1, le=100)
    api_requests_per_minute_per_user: int = Field(default=60, ge=1, le=10_000)
    api_max_inflight_per_user: int = Field(default=4, ge=1, le=100)
    api_requests_per_minute_per_tenant: int = Field(default=600, ge=1, le=100_000)
    api_max_inflight_per_tenant: int = Field(default=40, ge=1, le=1_000)
    # JSON object mapping tenant id -> per-tenant limit overrides, e.g.
    # HARBORRAG_API_TENANT_CAPACITY_OVERRIDES='{"acme":{"tenant_requests_per_minute":6000}}'
    api_tenant_capacity_overrides: dict[str, TenantCapacityOverride] = {}
    api_request_timeout_seconds: float = Field(default=120.0, ge=1.0, le=900.0)
    # Wall-clock budget for one SSE body (``stream: true`` completions). The
    # request timeout above covers only the handler, so a stream that outlives
    # this budget ends with one in-band ``event: error`` frame instead of a
    # truncated body.
    api_stream_timeout_seconds: float = Field(default=600.0, ge=1.0, le=3600.0)
    # Total prompt+completion token budget for one agent run (all steps).
    api_agent_token_budget: int = Field(default=120_000, ge=4_096, le=2_000_000)
    docs_enabled: bool = True

    @model_validator(mode="before")
    @classmethod
    def _default_docs_disabled_in_prod(cls, data: Any) -> Any:
        """Disable Swagger/OpenAPI docs by default in prod unless requested.

        ``docs_enabled`` defaults to True for local/dev ergonomics, but that
        default must not silently expose the API schema in production. If
        ``env`` resolves to "prod" and the operator never set
        ``docs_enabled``/``HARBORRAG_DOCS_ENABLED`` explicitly, this flips
        the effective default to False; an explicit value (either way) is
        always respected.
        """
        if isinstance(data, dict) and data.get("env") == "prod" and "docs_enabled" not in data:
            data = {**data, "docs_enabled": False}
        return data

    @model_validator(mode="after")
    def _validate_capacity_backend(self) -> ApiSettings:
        if self.env == "prod" and self.api_capacity_redis_url is None:
            raise ValueError("HARBORRAG_API_CAPACITY_REDIS_URL is required when HARBORRAG_ENV=prod")
        if self.api_capacity_redis_url is not None:
            try:
                _CAPACITY_REDIS_TRANSPORT.validate(
                    self.api_capacity_redis_url.get_secret_value(),
                    allow_insecure_remote=(
                        self.env == "dev" and self.api_capacity_allow_insecure_remote
                    ),
                )
            except ValueError as exc:
                raise ValueError(f"HARBORRAG_API_CAPACITY_REDIS_URL: {exc}") from exc
        return self

    def default_capacity_limits(self) -> CapacityLimits:
        """The three-tier limits every tenant without an override gets."""
        return CapacityLimits(
            user=CapacityTierLimits(
                self.api_requests_per_minute_per_user,
                self.api_max_inflight_per_user,
            ),
            principal=CapacityTierLimits(
                self.api_requests_per_minute,
                self.api_max_inflight_per_principal,
            ),
            tenant=CapacityTierLimits(
                self.api_requests_per_minute_per_tenant,
                self.api_max_inflight_per_tenant,
            ),
        )

    def tenant_capacity_limits(self) -> dict[str, CapacityLimits]:
        """Resolve each configured tenant override against the defaults."""
        defaults = self.default_capacity_limits()
        return {
            tenant_id: _apply_override(defaults, override)
            for tenant_id, override in self.api_tenant_capacity_overrides.items()
        }

    @model_validator(mode="after")
    def _validate_capacity_limits(self) -> ApiSettings:
        """A tenant aggregate below one member's share is unreachable config."""
        _check_tenant_ceiling(self.default_capacity_limits(), "the defaults")
        for tenant_id, override in self.api_tenant_capacity_overrides.items():
            _check_tenant_ceiling(
                _apply_override(self.default_capacity_limits(), override),
                f"tenant {tenant_id!r}",
            )
        return self


def _apply_override(
    defaults: CapacityLimits,
    override: TenantCapacityOverride,
) -> CapacityLimits:
    return CapacityLimits(
        user=CapacityTierLimits(
            override.user_requests_per_minute or defaults.user.requests_per_minute,
            override.user_max_inflight or defaults.user.max_inflight,
        ),
        principal=CapacityTierLimits(
            override.requests_per_minute or defaults.principal.requests_per_minute,
            override.max_inflight or defaults.principal.max_inflight,
        ),
        tenant=CapacityTierLimits(
            override.tenant_requests_per_minute or defaults.tenant.requests_per_minute,
            override.tenant_max_inflight or defaults.tenant.max_inflight,
        ),
    )


def _check_tenant_ceiling(limits: CapacityLimits, subject: str) -> None:
    member_rate = max(limits.user.requests_per_minute, limits.principal.requests_per_minute)
    if limits.tenant.requests_per_minute < member_rate:
        raise ValueError(
            f"API capacity for {subject}: the per-tenant request rate "
            f"({limits.tenant.requests_per_minute}/min) is below the per-user or "
            f"per-principal rate ({member_rate}/min); raise the per-tenant "
            "requests_per_minute or lower the per-user/per-principal one"
        )
    member_inflight = max(limits.user.max_inflight, limits.principal.max_inflight)
    if limits.tenant.max_inflight < member_inflight:
        raise ValueError(
            f"API capacity for {subject}: the per-tenant concurrency ceiling "
            f"({limits.tenant.max_inflight}) is below the per-user or per-principal "
            f"ceiling ({member_inflight}); raise the per-tenant max_inflight or "
            "lower the per-user/per-principal one"
        )
