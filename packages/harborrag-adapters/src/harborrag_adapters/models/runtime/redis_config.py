from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .security import SecretValue, reveal_secret


class RedisConnectionConfig(BaseModel):
    """Configure a shared Redis connection used by distributed model services."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: SecretValue
    key_prefix: str = Field(default="harborrag:models", min_length=1, max_length=128)
    max_connections: int = Field(default=100, ge=1, le=10_000)
    socket_timeout_seconds: float = Field(default=5.0, gt=0, le=300)
    socket_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=300)
    health_check_interval_seconds: int = Field(default=30, ge=0, le=3_600)
    decode_responses: bool = True

    @model_validator(mode="after")
    def validate_url(self) -> RedisConnectionConfig:
        """Require a Redis URL while retaining password redaction in configuration output."""

        value = reveal_secret(self.url)
        if value is not None and not value.startswith(("redis://", "rediss://", "unix://")):
            raise ValueError("redis.url must use redis://, rediss://, or unix://")
        return self

    def resolved_url(self) -> str:
        """Return the resolved Redis URL for the optional redis-py boundary."""

        value = reveal_secret(self.url)
        if value is None:
            raise ValueError("redis.url is required")
        return value
