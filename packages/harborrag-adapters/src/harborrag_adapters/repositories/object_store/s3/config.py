from typing import Literal

from pydantic import SecretStr

from harborrag_adapters.repositories.plugin import RepositoryConfig


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
