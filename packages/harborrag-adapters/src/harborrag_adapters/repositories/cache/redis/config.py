from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import SecretStr, model_validator

from harborrag_adapters.repositories.plugin import RepositoryConfig
from harborrag_core.security import RemoteTransportPolicy

_REDIS_TRANSPORT = RemoteTransportPolicy(
    service="Redis cache",
    allowed_schemes=frozenset({"redis", "rediss"}),
    secure_schemes=frozenset({"rediss"}),
)


class RedisCacheConfig(RepositoryConfig):
    """Configures Redis caching, counters, tags, and lease-based locks."""

    backend: Literal["redis"] = "redis"
    url: SecretStr
    key_prefix: str = "harborrag:v1"
    allow_insecure_remote: bool = False

    @model_validator(mode="after")
    def validate_transport(self) -> RedisCacheConfig:
        value = self.url.get_secret_value()
        parsed = urlsplit(value)
        if parsed.scheme == "unix" and parsed.path:
            return self
        _REDIS_TRANSPORT.validate(
            value,
            allow_insecure_remote=self.allow_insecure_remote,
        )
        return self
