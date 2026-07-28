from __future__ import annotations

from ipaddress import ip_address
from typing import Literal

from pydantic import Field, SecretStr, model_validator

from harborrag_adapters.repositories.plugin import RepositoryConfig


class FalkorDBGraphConfig(RepositoryConfig):
    """Configures the official async FalkorDB client and selected graph key."""

    backend: Literal["falkordb"] = "falkordb"
    host: str = "localhost"
    port: int = Field(default=6379, ge=1, le=65535)
    username: str | None = None
    password: SecretStr | None = None
    graph_name: str = "harborrag"
    ssl: bool = False
    max_connections: int = Field(default=32, ge=1, le=1000)
    allow_insecure_remote: bool = False

    @model_validator(mode="after")
    def validate_transport(self) -> FalkorDBGraphConfig:
        loopback = _is_loopback(self.host)
        if self.password is not None and not self.ssl and not loopback:
            raise ValueError("FalkorDB password cannot be sent over plaintext transport")
        if not self.ssl and not loopback and not self.allow_insecure_remote:
            raise ValueError(
                "remote FalkorDB requires SSL; set allow_insecure_remote only "
                "for an explicitly trusted development network"
            )
        return self


def _is_loopback(host: str) -> bool:
    normalized = host.rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False
