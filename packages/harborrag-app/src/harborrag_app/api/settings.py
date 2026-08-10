"""API process settings, loaded from HARBORRAG_* environment variables (ST2).

Static process config only (ports, auth mode, CORS); resource configuration
(projects/sources/providers) is DB-backed and API-mutable per plan §4.3.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from harborrag_core.security import RemoteTransportPolicy

AuthMode = Literal["none", "hmac", "oidc"]
_CAPACITY_REDIS_TRANSPORT = RemoteTransportPolicy(
    service="API capacity Redis",
    allowed_schemes=frozenset({"redis", "rediss"}),
    secure_schemes=frozenset({"rediss"}),
)


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
    max_request_body_bytes: int = Field(default=1_048_576, ge=1_024, le=16_777_216)
    api_capacity_redis_url: SecretStr | None = None
    api_capacity_allow_insecure_remote: bool = False
    api_requests_per_minute: int = Field(default=60, ge=1, le=10_000)
    api_max_inflight_per_principal: int = Field(default=4, ge=1, le=100)
    api_request_timeout_seconds: float = Field(default=120.0, ge=1.0, le=900.0)
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
