from __future__ import annotations

from typing import Literal

from pydantic import SecretStr, model_validator

from harborrag_adapters.repositories.plugin import RepositoryConfig
from harborrag_core.security import RemoteTransportPolicy

_S3_TRANSPORT = RemoteTransportPolicy(
    service="S3 object storage",
    allowed_schemes=frozenset({"http", "https"}),
    secure_schemes=frozenset({"https"}),
)


class S3ObjectStoreConfig(RepositoryConfig):
    """Configures S3-compatible object storage."""

    backend: Literal["s3"] = "s3"
    endpoint_url: str | None = None
    region: str | None = None
    access_key_id: SecretStr | None = None
    secret_access_key: SecretStr | None = None
    session_token: SecretStr | None = None
    default_bucket: str | None = None
    server_side_encryption: str | None = None
    allow_insecure_remote: bool = False

    @model_validator(mode="after")
    def validate_endpoint_transport(self) -> S3ObjectStoreConfig:
        if self.endpoint_url is None:
            return self
        parsed = _S3_TRANSPORT.validate(
            self.endpoint_url,
            allow_insecure_remote=self.allow_insecure_remote,
        )
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "S3 endpoint_url must not contain credentials, query, or fragment data"
            )
        return self
