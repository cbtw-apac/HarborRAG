from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import SecretStr, model_validator

from harborrag_adapters.repositories.plugin import RepositoryConfig
from harborrag_core.security import RemoteTransportPolicy

_REDIS_TRANSPORT = RemoteTransportPolicy(
    service="Redis state",
    allowed_schemes=frozenset({"redis", "rediss"}),
    secure_schemes=frozenset({"rediss"}),
)


class RedisStateConfig(RepositoryConfig):
    """Configures Redis workflow state with tenant-aware key namespacing."""

    backend: Literal["redis"] = "redis"
    url: SecretStr
    key_prefix: str = "harborrag:v1:state"
    allow_insecure_remote: bool = False

    @model_validator(mode="after")
    def validate_transport(self) -> RedisStateConfig:
        value = self.url.get_secret_value()
        parsed = urlsplit(value)
        if parsed.scheme == "unix" and parsed.path:
            return self
        _REDIS_TRANSPORT.validate(
            value,
            allow_insecure_remote=self.allow_insecure_remote,
        )
        return self
