"""Regression checks for the local Temporal development topology."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEV_SCRIPT = ROOT / "scripts/deployment/dev.sh"
TEMPORAL_COMPOSE = ROOT / "deploy/compose/docker-compose.temporal.yml"


def test_worker_source_mount_comes_from_the_connector_environment() -> None:
    temporal = TEMPORAL_COMPOSE.read_text(encoding="utf-8")
    script = DEV_SCRIPT.read_text(encoding="utf-8")
    temporal_env_example = (ROOT / "env-example/.env.temporal.example").read_text(encoding="utf-8")

    assert "HARBORRAG_LOCAL_SOURCE_MOUNT" not in temporal
    assert "HARBORRAG_LOCAL_SOURCE_MOUNT" not in temporal_env_example
    assert "HARBORRAG_TEMPORAL_API_KEY=" in temporal_env_example
    assert "${HARBORRAG_LOCAL_SOURCE_DIR:-" in temporal
    assert "LOCAL_SOURCE_PATH: /data/sources" in temporal
    assert "s/^LOCAL_SOURCE_PATH=//p" in script
    assert "export HARBORRAG_LOCAL_SOURCE_DIR" in script


def test_worker_config_paths_are_absolute_container_paths() -> None:
    temporal = TEMPORAL_COMPOSE.read_text(encoding="utf-8")
    dockerfile = (ROOT / "deploy/docker/Dockerfile.temporal-worker").read_text(encoding="utf-8")

    assert "WORKDIR /var/lib/harborrag" in dockerfile
    assert "COPY config ./config" in dockerfile
    for name in ("TEMPORAL", "CONNECTOR", "PARSER", "MODEL"):
        variable = f"HARBORRAG_{name}_CONFIG_PATH"
        assert (
            f"ENV {variable}=/app/config/" in dockerfile or f"{variable}=/app/config/" in dockerfile
        )
        assert f"{variable}: /app/config/" in temporal
        assert f"${{{variable}:-" not in temporal
    assert "../../config:/app/config:ro" not in temporal


def test_api_mounts_config_while_worker_uses_baked_runtime_configuration() -> None:
    api = (ROOT / "deploy/compose/docker-compose.yml").read_text(encoding="utf-8")
    temporal = TEMPORAL_COMPOSE.read_text(encoding="utf-8")
    dockerfile = (ROOT / "deploy/docker/Dockerfile.temporal-worker").read_text(encoding="utf-8")

    assert "../../config:/app/config:ro" in api
    assert "../../config:/app/config:ro" not in temporal
    assert "../../config/temporal.yaml:/app/config/temporal.yaml:ro" in temporal
    assert "COPY config ./config" in dockerfile


def test_temporal_secret_is_explicitly_scoped_to_api_and_worker() -> None:
    api = (ROOT / "deploy/compose/docker-compose.yml").read_text(encoding="utf-8")
    temporal = TEMPORAL_COMPOSE.read_text(encoding="utf-8")

    interpolation = 'HARBORRAG_TEMPORAL_API_KEY: "${HARBORRAG_TEMPORAL_API_KEY:-}"'
    assert interpolation in api
    assert interpolation in temporal


def test_temporal_compose_mounts_its_required_dynamic_config() -> None:
    temporal = TEMPORAL_COMPOSE.read_text(encoding="utf-8")
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
    assert f"harborrag-adapters[{adapter_extras}]==0.1.0" in runtime_project
    assert f"harborrag-adapters[{adapter_extras}]'" in dockerfile


def test_temporal_and_worker_subcommands_have_separate_ownership() -> None:
    script = DEV_SCRIPT.read_text(encoding="utf-8")
    temporal_function = script.split("start_temporal() {", 1)[1].split("start_worker() {", 1)[0]
    worker_function = script.split("start_worker() {", 1)[1].split("start_api() {", 1)[0]

    assert "--profile worker" not in temporal_function
    assert "temporal-worker" not in temporal_function
    assert "--profile worker" in worker_function
    assert "--no-deps" in worker_function
    assert "temporal-worker" in worker_function


def test_temporal_startup_and_dependents_require_cluster_health() -> None:
    temporal = TEMPORAL_COMPOSE.read_text(encoding="utf-8")
    script = DEV_SCRIPT.read_text(encoding="utf-8")
    require_function = script.split("require_temporal_server() {", 1)[1].split(
        "data_compose() {", 1
    )[0]
    temporal_function = script.split("start_temporal() {", 1)[1].split("start_worker() {", 1)[0]

    assert "temporal operator cluster health" in temporal
    assert "Temporal did not become healthy" in temporal
    assert "condition: service_completed_successfully" in temporal
    assert "run --rm --no-deps temporal-namespace" in require_function
    assert "ps --status running" not in require_function
    assert "up --detach temporal-schema temporal" in temporal_function
    assert "require_temporal_server" in temporal_function
    assert "up --detach temporal-namespace temporal-ui" in temporal_function
