from __future__ import annotations

from enum import StrEnum
from ipaddress import ip_address
from typing import Literal
from urllib.parse import urlsplit

from pydantic import SecretStr, model_validator

from harborrag_adapters.repositories.plugin import RepositoryConfig


class QdrantDeployment(StrEnum):
    """Describes whether the real Qdrant engine is embedded or remote."""

    EMBEDDED = "embedded"
    REMOTE = "remote"


class QdrantVectorConfig(RepositoryConfig):
    """Configures one Qdrant backend without an ambiguous local mode."""

    backend: Literal["qdrant"] = "qdrant"
    deployment: QdrantDeployment = QdrantDeployment.REMOTE
    url: str | None = None
    path: str | None = None
    api_key: SecretStr | None = None
    prefer_grpc: bool = True
    allow_insecure_remote: bool = False

    @model_validator(mode="after")
    def validate_deployment(self) -> QdrantVectorConfig:
        if self.deployment is QdrantDeployment.REMOTE:
            if not self.url:
                raise ValueError("remote Qdrant requires url")
            if self.path is not None:
                raise ValueError("remote Qdrant does not accept path")
            parsed = urlsplit(self.url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("remote Qdrant url must be an absolute HTTP(S) URL")
            # urlsplit and qdrant-client's urllib3 parser disagree on these, so
            # refuse them rather than validate a different host than is dialled.
            if (
                parsed.username
                or parsed.password
                or "@" in parsed.netloc
                or "\\" in self.url
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "Qdrant url must not contain credentials, backslashes, query, or fragment data"
                )
            if (
                parsed.scheme != "https"
                and not _is_loopback(parsed.hostname)
                and not self.allow_insecure_remote
            ):
                raise ValueError(
                    "remote Qdrant requires HTTPS; set allow_insecure_remote only "
                    "for an explicitly trusted development network"
                )
            # An api_key to a plaintext remote URL needs the same explicit
            # allow_insecure_remote acknowledgement checked above; on such a
            # trusted network an authenticated link is strictly safer than the
            # unauthenticated one refusing the key would force.
            return self
        if self.url is not None:
            raise ValueError("embedded Qdrant does not accept url")
        if self.api_key is not None:
            raise ValueError("embedded Qdrant does not accept api_key")
        return self


def _is_loopback(host: str) -> bool:
    normalized = host.rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False
