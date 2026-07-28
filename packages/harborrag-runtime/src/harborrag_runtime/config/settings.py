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


class RuntimeSettings(BaseSettings):
    """Environment-driven settings for runtime composition."""

    model_config = SettingsConfigDict(env_prefix="HARBORRAG_", extra="ignore")

    env: Literal["dev", "prod"] = "dev"
    control_db_url: str = "sqlite+aiosqlite:///./harborrag_control.db"
    temporal_target: str = "localhost:7233"
    temporal_namespace: str = "harborrag"
    temporal_identity: str = "harborrag-runtime"
    temporal_api_key: str | None = None
    temporal_tls: bool = False
    temporal_allow_insecure_remote: bool = False
    temporal_health_timeout_seconds: float = Field(default=5.0, gt=0)
    ingestion_partition_size: int = 50
    partition_concurrency: int = 4
    artifact_concurrency: int = 16
    isolated_process_concurrency: int = Field(default=2, ge=1, le=32)
    isolated_process_wall_seconds: float = Field(default=900.0, gt=0)
    isolated_process_cpu_seconds: int = Field(default=600, ge=1)
    isolated_process_memory_bytes: int = Field(
        default=8 * 1024 * 1024 * 1024,
        ge=512 * 1024 * 1024,
    )
    isolated_process_file_bytes: int = Field(
        default=512 * 1024 * 1024,
        ge=1024 * 1024,
    )
    temporal_dependency_provider: str | None = None
    temporal_worker_groups: str = "discovery,processing,indexing,maintenance"
    connector_config_path: Path = Path("config/connectors.yaml")
    parser_config_path: Path = Path("config/parsers.yaml")
    model_config_path: Path = Path("config/models.yaml")
    ingestion_state_backend: Literal["postgresql"] = "postgresql"
    ingestion_state_url: SecretStr | None = None
    ingestion_state_pool_size: int = Field(default=5, ge=1, le=100)
    ingestion_state_max_overflow: int = Field(default=10, ge=0, le=200)
    ingestion_state_allow_insecure_remote: bool = False
    ingestion_object_root: Path = Path(".harborrag/objects")
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None
    qdrant_prefer_grpc: bool = True
    qdrant_collection_prefix: str = ""
    qdrant_allow_insecure_remote: bool = False
    falkordb_host: str = "localhost"
    falkordb_port: int = 6379
    falkordb_username: str | None = None
    falkordb_password: str | None = None
    falkordb_graph: str = "harborrag"
    falkordb_ssl: bool = False
    falkordb_allow_insecure_remote: bool = False
    embedding_model: str | None = None
    embedding_dimensions: int | None = None
    vector_collection: str = "harborrag_chunks"
    graph_namespace: str = "harborrag"

    @model_validator(mode="after")
    def validate_ingestion_state(self) -> RuntimeSettings:
        if (
            self.ingestion_state_url is not None
            and not self.ingestion_state_url.get_secret_value().startswith("postgresql+asyncpg://")
        ):
            raise ValueError("HARBORRAG_INGESTION_STATE_URL must use postgresql+asyncpg")
        return self

    def require_postgresql_ingestion_state(self) -> SecretStr:
        """Return the configured shared state URL for a built-in Temporal worker."""
        if self.ingestion_state_url is None:
            raise ValueError(
                "built-in Temporal workers require PostgreSQL ingestion state; set "
                "HARBORRAG_INGESTION_STATE_URL"
            )
        return self.ingestion_state_url


DEFAULT_CONTROL_DB_URL = RuntimeSettings.model_fields["control_db_url"].default
