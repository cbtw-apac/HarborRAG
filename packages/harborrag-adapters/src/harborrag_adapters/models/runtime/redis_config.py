from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from harborrag_core.security import RemoteTransportPolicy

from .security import SecretValue, reveal_secret

_REDIS_TRANSPORT = RemoteTransportPolicy(
    service="model runtime Redis",
    allowed_schemes=frozenset({"redis", "rediss"}),
    secure_schemes=frozenset({"rediss"}),
)


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
    allow_insecure_remote: bool = False

    @model_validator(mode="after")
    def validate_url(self) -> RedisConnectionConfig:
        """Require a Redis URL while retaining password redaction in configuration output."""

        value = reveal_secret(self.url)
        if value is None:
            return self
        parsed = urlsplit(value)
        if parsed.scheme == "unix" and parsed.path:
            return self
        try:
            _REDIS_TRANSPORT.validate(
                value,
                allow_insecure_remote=self.allow_insecure_remote,
            )
        except ValueError as exc:
            raise ValueError(f"redis.url: {exc}") from exc
        return self

    def resolved_url(self) -> str:
        """Return the resolved Redis URL for the optional redis-py boundary."""

        value = reveal_secret(self.url)
        if value is None:
            raise ValueError("redis.url is required")
        return value
