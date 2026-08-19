"""Runtime process settings from HARBORRAG_* env vars (ST8).

Imported lazily by CompositionRoot.production() only — pydantic-settings is
part of the [production] extra, and the bare CLI install must keep working
without it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from harborrag_core.invariants import HarborInvariantError
from harborrag_core.security import RemoteTransportPolicy

_REDIS_TRANSPORT = RemoteTransportPolicy(
    service="Redis",
    allowed_schemes=frozenset({"redis", "rediss"}),
    secure_schemes=frozenset({"rediss"}),
)
_OBJECT_STORE_TRANSPORT = RemoteTransportPolicy(
    service="object store",
    allowed_schemes=frozenset({"http", "https"}),
    secure_schemes=frozenset({"https"}),
)


def is_blank_secret(value: SecretStr | None) -> bool:
    """True for an unset secret or one that is empty/whitespace-only.

    A blank string is not None, so it would otherwise slip past an
    `is None` check and reach the encryption key derivation as a fixed,
    publicly-guessable value -- weaker than even the dev-default key.
    """
    return value is None or not value.get_secret_value().strip()


class RuntimeSettings(BaseSettings):
    """Environment-driven settings for runtime composition."""

    model_config = SettingsConfigDict(env_prefix="HARBORRAG_", extra="ignore")

    env: Literal["dev", "prod"] = "dev"
    ingestion_tenant_id: str = Field(
        default="DEFAULT",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    control_db_url: SecretStr = SecretStr("sqlite+aiosqlite:///./harborrag_control.db")
    control_db_pool_size: int = Field(default=5, ge=1, le=100)
    control_db_max_overflow: int = Field(default=10, ge=0, le=200)
    secrets_encryption_key: SecretStr | None = None
    temporal_target: str = "localhost:7233"
    temporal_namespace: str = "harborrag"
    temporal_identity: str = "harborrag-runtime"
    temporal_worker_identity: str = "harborrag-runtime"
    temporal_api_key: SecretStr | None = None
    temporal_tls: bool = False
    temporal_allow_insecure_remote: bool = False
    temporal_health_timeout_seconds: float = Field(default=5.0, gt=0)
    temporal_max_concurrent_activities: int = Field(default=2, ge=1, le=1000)
    temporal_max_concurrent_workflow_tasks: int = Field(default=4, ge=1, le=1000)
    temporal_max_concurrent_activity_polls: int = Field(default=2, ge=1, le=100)
    temporal_max_concurrent_workflow_polls: int = Field(default=2, ge=1, le=100)
    temporal_graceful_shutdown_seconds: int = Field(default=30, ge=1, le=3600)
    temporal_ingestion_batch_size: int = Field(default=200, ge=1, le=300)
    temporal_ingestion_document_concurrency: int = Field(default=8, ge=1, le=100)
    temporal_config_path: Path = Path("config/temporal.yaml")
    metrics_port: int | None = Field(default=None, ge=1, le=65_535)
    metrics_bind_address: str = Field(default="0.0.0.0", min_length=1)
    langfuse_enabled: bool = False
    redis_url: SecretStr | None = None
    redis_allow_insecure_remote: bool = False
    redis_socket_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    connector_rate_limit_key_prefix: str = Field(
        default="harborrag-connector-rate",
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    connector_config_path: Path = Path("config/connectors.yaml")
    parser_config_path: Path = Path("config/parsers.yaml")
    model_config_path: Path = Path("config/models.yaml")
    object_store_endpoint_url: str | None = "http://localhost:9000"
    object_store_allow_insecure_remote: bool = False
    object_store_region: str = "us-east-1"
    object_store_access_key_id: SecretStr | None = None
    object_store_secret_access_key: SecretStr | None = None
    object_store_session_token: SecretStr | None = None
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: SecretStr | None = None
    qdrant_prefer_grpc: bool = True
    qdrant_collection_prefix: str = ""
    qdrant_allow_insecure_remote: bool = False
    falkordb_host: str = "localhost"
    falkordb_port: int = 6379
    falkordb_username: str | None = None
    falkordb_password: SecretStr | None = None
    falkordb_graph: str = "harborrag"
    falkordb_ssl: bool = False
    falkordb_max_connections: int = Field(default=32, ge=1, le=1000)
    graph_relation_repair_concurrency: int = Field(default=8, ge=1, le=1000)
    falkordb_allow_insecure_remote: bool = False
    embedding_model: str | None = None
    embedding_dimensions: int | None = None
    dense_encoder_profile: str = "dense-default-v1"
    sparse_encoder_profile: str = "bm25-hashed-v1"
    sparse_k: float = Field(default=1.2, gt=0)
    sparse_b: float = Field(default=0.75, ge=0, le=1)
    sparse_fixed_avg_len: float = Field(default=256.0, gt=0)
    retrieval_dense_weight: float = Field(default=0.7, ge=0, le=1)
    chat_retrieval_top_k: int = Field(default=5, ge=1, le=50)
    chat_retrieval_graph_search: bool = False

    @model_validator(mode="after")
    def validate_secret_urls(self) -> RuntimeSettings:
        control_db_url = self.control_db_url.get_secret_value().lower()
        is_sqlite_control_db = control_db_url.startswith("sqlite")
        if self.env == "prod" and is_sqlite_control_db:
            raise ValueError(
                "HARBORRAG_CONTROL_DB_URL must use a production database when "
                "HARBORRAG_ENV=prod; SQLite is development-only"
            )
        if is_blank_secret(self.secrets_encryption_key) and not is_sqlite_control_db:
            # env=dev with a real (non-SQLite) control DB is a legal combination, and
            # it would otherwise silently encrypt stored secrets with the
            # publicly-known dev-default key -- require an explicit key for any
            # persistent control database, not only in prod.
            raise ValueError(
                "HARBORRAG_SECRETS_ENCRYPTION_KEY must be set when HARBORRAG_CONTROL_DB_URL "
                "is not SQLite; the dev-only default key is not safe for stored secrets"
            )
        development = self.env == "dev"
        if self.redis_url is not None:
            try:
                _REDIS_TRANSPORT.validate(
                    self.redis_url.get_secret_value(),
                    allow_insecure_remote=(development and self.redis_allow_insecure_remote),
                )
            except ValueError as exc:
                raise ValueError(f"HARBORRAG_REDIS_URL: {exc}") from exc
        if self.object_store_endpoint_url is not None:
            try:
                _OBJECT_STORE_TRANSPORT.validate(
                    self.object_store_endpoint_url,
                    allow_insecure_remote=(development and self.object_store_allow_insecure_remote),
                )
            except ValueError as exc:
                raise ValueError(f"HARBORRAG_OBJECT_STORE_ENDPOINT_URL: {exc}") from exc
        return self


_DEFAULT_CONTROL_DB_SECRET = RuntimeSettings.model_fields["control_db_url"].default
if not isinstance(_DEFAULT_CONTROL_DB_SECRET, SecretStr):
    raise HarborInvariantError("control_db_url default must be a SecretStr")
DEFAULT_CONTROL_DB_URL = _DEFAULT_CONTROL_DB_SECRET.get_secret_value()
