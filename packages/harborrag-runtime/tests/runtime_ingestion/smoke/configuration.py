"""Secret-safe configuration for the deployed ingestion smoke run."""

from __future__ import annotations

import os
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from pydantic import SecretStr

from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.ingestion.document.normalization import (
    CANONICAL_NORMALIZER_VERSION,
)
from harborrag_runtime.temporal.schemas import (
    ProcessingProfileInput,
    SourceIngestionInput,
)

ROOT = Path(__file__).resolve().parents[5]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
TENANT_ID = "ingestion-smoke"
SOURCE_SCOPE_ID = "local-ingestion-smoke"
CONNECTION_ID = "local-ingestion-smoke"


@dataclass(frozen=True, slots=True)
class SmokeConfiguration:
    settings: RuntimeSettings
    configuration_fingerprint: str
    processing: ProcessingProfileInput

    def source_input(self, task_id: str) -> SourceIngestionInput:
        return SourceIngestionInput(
            task_id=task_id,
            tenant_id=TENANT_ID,
            connector_name="harborrag-workspace",
            connector_type="local",
            connection_id=CONNECTION_ID,
            source_scope_id=SOURCE_SCOPE_ID,
            configuration_fingerprint=self.configuration_fingerprint,
            processing=self.processing,
            document_concurrency=2,
            batch_size=100,
        )


def load_smoke_configuration() -> SmokeConfiguration:
    """Load ignored env files without exposing or replacing exported secrets."""

    for relative in (
        "env/.env.database",
        "env/.env.temporal",
        "env/.env.models",
        "env/.env.connector",
    ):
        load_dotenv(ROOT / relative, override=False)
    database_url = _postgres_url()
    settings = RuntimeSettings(
        env="prod",
        control_db_url=database_url,
        temporal_target=f"127.0.0.1:{_env('TEMPORAL_PORT', '7233')}",
        temporal_namespace=_env(
            "HARBORRAG_TEMPORAL_NAMESPACE",
            "harborrag",
        ),
        temporal_identity="harborrag-ingestion-smoke",
        redis_url=SecretStr(f"redis://127.0.0.1:{_env('REDIS_PORT', '6380')}/0"),
        connector_config_path=ROOT / "config/connectors.yaml",
        parser_config_path=ROOT / "config/parsers.yaml",
        model_config_path=ROOT / "config/models.yaml",
        object_store_endpoint_url=(f"http://127.0.0.1:{_env('MINIO_API_PORT', '9000')}"),
        object_store_access_key_id=_required("MINIO_ROOT_USER"),
        object_store_secret_access_key=_required("MINIO_ROOT_PASSWORD"),
        qdrant_url=(f"http://127.0.0.1:{_env('QDRANT_HTTP_PORT', '6333')}"),
        qdrant_prefer_grpc=False,
        falkordb_host="127.0.0.1",
        falkordb_port=int(_env("FALKORDB_PORT", "6379")),
    )
    return SmokeConfiguration(
        settings=settings,
        configuration_fingerprint=_fixture_fingerprint(),
        processing=ProcessingProfileInput(
            parser_profile=_file_fingerprint(
                "parser",
                settings.parser_config_path,
            ),
            normalizer_version=CANONICAL_NORMALIZER_VERSION,
            chunk_strategy="route-evidence-v4",
            dense_encoder_profile=settings.dense_encoder_profile,
            sparse_encoder_profile=settings.sparse_encoder_profile,
            graph_projection_version="structural-graph-v1",
        ),
    )


def _postgres_url() -> str:
    username = quote(_required("POSTGRES_USER"), safe="")
    password = quote(_required("POSTGRES_PASSWORD"), safe="")
    database = quote(_required("POSTGRES_DB"), safe="")
    port = _env("POSTGRES_PORT", "5432")
    return f"postgresql+asyncpg://{username}:{password}@127.0.0.1:{port}/{database}"


def _fixture_fingerprint() -> str:
    digest = sha256()
    for path in sorted(FIXTURES.glob("*.md")):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"local-smoke-{digest.hexdigest()[:24]}"


def _file_fingerprint(prefix: str, path: Path) -> str:
    return f"{prefix}-{sha256(path.read_bytes()).hexdigest()[:16]}"


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"smoke environment variable is missing: {name}")
    return value


def _env(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value or default
