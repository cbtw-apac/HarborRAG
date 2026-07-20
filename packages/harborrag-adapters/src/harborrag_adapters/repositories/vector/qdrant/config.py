from __future__ import annotations

from enum import StrEnum
from typing import Literal

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
    collection_prefix: str = ""

    @model_validator(mode="after")
    def validate_deployment(self) -> QdrantVectorConfig:
        if self.deployment is QdrantDeployment.REMOTE:
            if not self.url:
                raise ValueError("remote Qdrant requires url")
            if self.path is not None:
                raise ValueError("remote Qdrant does not accept path")
            return self
        if self.url is not None:
            raise ValueError("embedded Qdrant does not accept url")
        if self.api_key is not None:
            raise ValueError("embedded Qdrant does not accept api_key")
        return self
