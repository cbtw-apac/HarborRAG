"""Regression checks for the supported local development topology."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API_COMPOSE = ROOT / "deploy/compose/docker-compose.yml"
DEV_SCRIPT = ROOT / "scripts/deployment/dev.sh"


def test_dev_api_uses_the_temporal_development_network() -> None:
    compose = API_COMPOSE.read_text(encoding="utf-8")

    assert "dockerfile: deploy/docker/Dockerfile.api" in compose
    assert 'HARBORRAG_TEMPORAL_TARGET: "${HARBORRAG_TEMPORAL_TARGET:-temporal:7233}"' in compose
    assert (
        "HARBORRAG_TEMPORAL_ALLOW_INSECURE_REMOTE: "
        '"${HARBORRAG_TEMPORAL_ALLOW_INSECURE_REMOTE:-true}"'
    ) in compose
    assert "harborrag-data-network" in compose
    assert "external: true" in compose
    assert "../../.env" not in compose


def test_dev_api_can_register_tasks_without_provider_secrets() -> None:
    compose = API_COMPOSE.read_text(encoding="utf-8")
    script = DEV_SCRIPT.read_text(encoding="utf-8")

    assert "HARBORRAG_CONTROL_DB_URL: postgresql+asyncpg://" in compose
    assert "LOCAL_SOURCE_PATH: /data/sources" in compose
    assert "CONFLUENCE_BASE_URL:" in compose
    assert "JIRA_BASE_URL:" in compose
    assert "CONFLUENCE_TOKEN" not in compose
    assert "JIRA_TOKEN" not in compose
    assert 'CONNECTOR_ENV_FILE="${CONNECTOR_ENV_FILE:-env/.env.connector}"' in script


def test_dev_api_binds_to_loopback_by_default() -> None:
    compose = API_COMPOSE.read_text(encoding="utf-8")

    assert "HARBORRAG_API_BIND_ADDRESS:-127.0.0.1" in compose
    assert "HARBORRAG_API_PORT:-8000" in compose


def test_data_and_temporal_ports_bind_to_loopback_by_default() -> None:
    database = (ROOT / "deploy/compose/docker-compose.database.yml").read_text(encoding="utf-8")
    temporal = (ROOT / "deploy/compose/docker-compose.temporal.yml").read_text(encoding="utf-8")

    assert database.count("HARBORRAG_DATA_BIND_ADDRESS:-127.0.0.1") == 8
    assert temporal.count("HARBORRAG_TEMPORAL_BIND_ADDRESS:-127.0.0.1") == 2


def test_compose_files_require_operator_supplied_passwords() -> None:
    database = (ROOT / "deploy/compose/docker-compose.database.yml").read_text(encoding="utf-8")
    monitoring = (ROOT / "deploy/compose/docker-compose.monitoring.yml").read_text(encoding="utf-8")

    assert "POSTGRES_PASSWORD:?" in database
    assert "MINIO_ROOT_PASSWORD:?" in database
    assert "GRAFANA_ADMIN_PASSWORD:?" in monitoring
    assert "postgres-change-me" not in database
    assert "minio-change-me" not in database
    assert "GRAFANA_ADMIN_PASSWORD:-admin" not in monitoring


def test_monitoring_is_private_authenticated_and_version_pinned() -> None:
    monitoring = (ROOT / "deploy/compose/docker-compose.monitoring.yml").read_text(encoding="utf-8")

    assert monitoring.count("HARBORRAG_MONITORING_BIND_ADDRESS:-127.0.0.1") == 2
    assert "GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD:?" in monitoring
    assert 'GF_AUTH_ANONYMOUS_ENABLED: "false"' in monitoring
    assert 'GF_USERS_ALLOW_SIGN_UP: "false"' in monitoring
    assert 'expose:\n      - "3100"' in monitoring
    assert '"3100:3100"' not in monitoring
    assert "image: prom/prometheus:v3.13.1" in monitoring
    assert "image: grafana/grafana:13.1.0" in monitoring
    assert "image: grafana/loki:3.7.2" in monitoring
    assert "@sha256:" not in monitoring
    assert ":latest" not in monitoring
    assert "internal: true" in monitoring
    assert "grafana_secure_data:/var/lib/grafana" in monitoring
    assert "grafana_data:/var/lib/grafana" not in monitoring


def test_falkordb_constraints_use_restart_safe_snapshot_persistence() -> None:
    database = (ROOT / "deploy/compose/docker-compose.database.yml").read_text(encoding="utf-8")
    falkordb_service = database.split("  falkordb:", 1)[1].split("  redis:", 1)[0]

    # FalkorDB 4.20.1 records asynchronous GRAPH.CONSTRAINT commands in AOF,
    # then crashes while replaying them on restart. RDB preserves both the
    # graph and operational constraints without replaying the command.
    assert 'REDIS_ARGS: "--appendonly no --save 60 1"' in falkordb_service
    assert "--appendonly yes" not in falkordb_service


def test_worker_source_mount_comes_from_the_connector_environment() -> None:
    temporal = (ROOT / "deploy/compose/docker-compose.temporal.yml").read_text(encoding="utf-8")
    script = DEV_SCRIPT.read_text(encoding="utf-8")
    temporal_env_example = (ROOT / "env-example/.env.temporal.example").read_text(encoding="utf-8")

    # The mount is not a separate knob: the startup script derives it from
    # LOCAL_SOURCE_PATH in env/.env.connector.
    assert "HARBORRAG_LOCAL_SOURCE_MOUNT" not in temporal
    assert "HARBORRAG_LOCAL_SOURCE_MOUNT" not in temporal_env_example
    assert "${HARBORRAG_LOCAL_SOURCE_DIR:-" in temporal
    assert "LOCAL_SOURCE_PATH: /data/sources" in temporal
    assert "s/^LOCAL_SOURCE_PATH=//p" in script
    assert "export HARBORRAG_LOCAL_SOURCE_DIR" in script


def test_worker_config_paths_are_absolute_container_paths() -> None:
    temporal = (ROOT / "deploy/compose/docker-compose.temporal.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "deploy/docker/Dockerfile.temporal-worker").read_text(encoding="utf-8")

    # The image copies config/ to /app/config but runs from /var/lib/harborrag,
    # so repository-relative configuration paths cannot resolve.
    assert "WORKDIR /var/lib/harborrag" in dockerfile
    assert "COPY config ./config" in dockerfile
    for name in ("CONNECTOR", "PARSER", "MODEL"):
        variable = f"HARBORRAG_{name}_CONFIG_PATH"
        assert (
            f"ENV {variable}=/app/config/" in dockerfile or f"{variable}=/app/config/" in dockerfile
        )
        assert f"{variable}: /app/config/" in temporal
        assert f"${{{variable}:-" not in temporal
    assert "../../config:/app/config:ro" not in temporal


def test_api_mounts_config_while_worker_uses_its_baked_runtime_configuration() -> None:
    api = API_COMPOSE.read_text(encoding="utf-8")
    temporal = (ROOT / "deploy/compose/docker-compose.temporal.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "deploy/docker/Dockerfile.temporal-worker").read_text(encoding="utf-8")

    assert "../../config:/app/config:ro" in api
    assert "../../config:/app/config:ro" not in temporal
    assert "COPY config ./config" in dockerfile


def test_temporal_compose_mounts_its_required_dynamic_config() -> None:
    temporal = (ROOT / "deploy/compose/docker-compose.temporal.yml").read_text(encoding="utf-8")
    dynamic_config = ROOT / "deploy/temporal/dynamicconfig/development-sql.yaml"

    assert "DYNAMIC_CONFIG_FILE_PATH: config/dynamicconfig/development-sql.yaml" in temporal
    assert "../temporal/dynamicconfig:/etc/temporal/config/dynamicconfig:ro" in temporal
    assert dynamic_config.is_file()


def test_worker_image_installs_durable_artifact_adapters() -> None:
    runtime_project = (ROOT / "packages/harborrag-runtime/pyproject.toml").read_text(
        encoding="utf-8"
    )
    dockerfile = (ROOT / "deploy/docker/Dockerfile.temporal-worker").read_text(encoding="utf-8")

    adapter_extras = (
        "chunking,control-plane,falkordb,langfuse,llm,opentelemetry,parsers,"
        "pdf-docling,postgres,qdrant,redis,s3,tables"
    )
    release_version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["version"]
    assert f"harborrag-adapters[{adapter_extras}]=={release_version}" in runtime_project
    assert f"harborrag-adapters[{adapter_extras}]'" in dockerfile
