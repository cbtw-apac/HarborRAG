"""Runtime process settings from HARBORRAG_* env vars (ST8).

Imported lazily by CompositionRoot.production() only — pydantic-settings is
part of the [production] extra, and the bare CLI install must keep working
without it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
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
    temporal_health_timeout_seconds: float = Field(default=5.0, gt=0)
    ingestion_partition_size: int = 50
    partition_concurrency: int = 4
    artifact_concurrency: int = 16
    temporal_dependency_provider: str | None = None
    temporal_worker_groups: str = "discovery,processing,indexing,maintenance"
    connector_config_path: Path = Path("config/connectors.yaml")
    parser_config_path: Path = Path("config/parsers.yaml")
    model_config_path: Path = Path("config/models.yaml")
    ingestion_state_database: Path = Path(".harborrag/ingestion-state.db")
    ingestion_object_root: Path = Path(".harborrag/objects")
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None
    qdrant_prefer_grpc: bool = True
    qdrant_collection_prefix: str = ""
    falkordb_host: str = "localhost"
    falkordb_port: int = 6379
    falkordb_username: str | None = None
    falkordb_password: str | None = None
    falkordb_graph: str = "harborrag"
    falkordb_ssl: bool = False
    embedding_model: str | None = None
    embedding_dimensions: int | None = None
    vector_collection: str = "harborrag_chunks"
    graph_namespace: str = "harborrag"


DEFAULT_CONTROL_DB_URL = RuntimeSettings.model_fields["control_db_url"].default
