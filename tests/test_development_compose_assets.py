"""Regression checks for the supported local development topology."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API_COMPOSE = ROOT / "deploy/compose/docker-compose.yml"
DEV_SCRIPT = ROOT / "scripts/deployment/dev.sh"
MCP_SCRIPT = ROOT / "scripts/deployment/mcp.sh"


def test_dev_api_uses_the_temporal_development_network() -> None:
    compose = API_COMPOSE.read_text(encoding="utf-8")

    assert "dockerfile: deploy/docker/Dockerfile.api" in compose
    assert "HARBORRAG_TEMPORAL_TARGET: temporal:7233" in compose
    assert 'HARBORRAG_TEMPORAL_ALLOW_INSECURE_REMOTE: "true"' in compose
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
    assert f"harborrag-adapters[{adapter_extras}]==0.1.0" in runtime_project
    assert f"harborrag-adapters[{adapter_extras}]'" in dockerfile


def test_development_entrypoint_orchestrates_explicit_components() -> None:
    script = DEV_SCRIPT.read_text(encoding="utf-8")

    assert ".env.example" not in script
    assert "env-example/.env.database.example" in script
    assert "env-example/.env.temporal.example" in script
    assert "start_data" in script
    assert "start_temporal" in script
    assert "start_worker" in script
    assert "start_api" in script
    assert "start_monitoring" not in script
    assert "monitoring_compose" not in script
    assert "    monitoring)" not in script
    assert "up [--no-worker] [--build]" in script
    assert "chmod 600" in script


def test_api_and_worker_reuse_local_images_unless_rebuild_is_requested() -> None:
    api = API_COMPOSE.read_text(encoding="utf-8")
    temporal = (ROOT / "deploy/compose/docker-compose.temporal.yml").read_text(encoding="utf-8")
    script = DEV_SCRIPT.read_text(encoding="utf-8")

    assert "image: ${HARBORRAG_API_IMAGE:-harborrag-api-api}" in api
    assert (
        "image: ${HARBORRAG_TEMPORAL_WORKER_IMAGE:-harborrag-temporal-temporal-worker}" in temporal
    )
    assert 'docker image inspect "${API_IMAGE}"' in script
    assert 'docker image inspect "${TEMPORAL_WORKER_IMAGE}"' in script
    assert "local -a build_args=(--no-build)" in script
    assert "build_args=(--build)" in script
    assert "api [--build]" in script
    assert "worker [--build]" in script


def test_monitoring_credentials_are_configured_outside_the_development_script() -> None:
    script = DEV_SCRIPT.read_text(encoding="utf-8")
    example = (ROOT / "env-example/.env.monitoring.example").read_text(encoding="utf-8")

    assert "MONITORING_ENV_FILE" not in script
    assert "ensure_monitoring_environment_file" not in script
    assert "env-example/.env.monitoring.example" not in script
    assert "HARBORRAG_MONITORING_BIND_ADDRESS=127.0.0.1" in example
    assert "GRAFANA_ADMIN_PASSWORD=\n" in example


def test_api_subcommand_validates_configuration_and_never_starts_worker() -> None:
    script = DEV_SCRIPT.read_text(encoding="utf-8")
    api_function = script.split("start_api() {", 1)[1].split("stop_stack() {", 1)[0]

    assert "config --services" in api_function
    assert '"${compose_services[0]:-}" != "api"' in api_function
    assert "--no-deps" in api_function
    assert "--wait" in api_function
    assert "--wait-timeout" in api_function
    assert "start_worker" not in api_function
    assert "temporal_compose" not in api_function


def test_mcp_entrypoint_runs_stdio_without_starting_other_processes() -> None:
    mcp_script = MCP_SCRIPT.read_text(encoding="utf-8")
    dev_script = DEV_SCRIPT.read_text(encoding="utf-8")

    assert "-m harborrag_mcp_server" in mcp_script
    assert "mcp_arguments+=(--check)" in mcp_script
    assert "HARBORRAG_CONTROL_DB_URL" in mcp_script
    assert "HARBORRAG_MODEL_CONFIG_PATH" in mcp_script
    assert "@localhost:" in mcp_script
    assert "http://localhost:" in mcp_script
    assert "start_worker" not in mcp_script
    assert "start_api" not in mcp_script
    assert "docker compose" not in mcp_script
    assert "start_mcp" not in dev_script
    assert "    mcp)" not in dev_script


def test_mcp_entrypoint_redacts_malformed_environment_values(tmp_path: Path) -> None:
    project = tmp_path / "project"
    script = project / "scripts/deployment/mcp.sh"
    environment = project / "env"
    script.parent.mkdir(parents=True)
    environment.mkdir()
    script.write_text(MCP_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    (environment / ".env.database").write_text("POSTGRES_USER=test\n", encoding="utf-8")
    fake_key = "sk-proj-" + "a" * 40
    (environment / ".env.models").write_text(
        f"HARBOR_CHAT_API_KEY= {fake_key}\n",
        encoding="utf-8",
    )
    (environment / ".env.api").write_text("HARBORRAG_AUTH_MODE=none\n", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(script), "--check"],
        cwd=project,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert fake_key not in result.stderr
    assert "line 1: ***: command not found" in result.stderr


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
    temporal = (ROOT / "deploy/compose/docker-compose.temporal.yml").read_text(encoding="utf-8")
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


def test_deployment_has_explicit_orchestration_and_mcp_entrypoints() -> None:
    scripts = sorted((ROOT / "scripts/deployment").glob("*.sh"))

    assert [script.name for script in scripts] == ["dev.sh", "mcp.sh"]
    assert all(script.stat().st_mode & stat.S_IXUSR for script in scripts)


def test_api_has_one_canonical_compose_file() -> None:
    assert API_COMPOSE.is_file()
    for obsolete_name in (
        "docker-compose.dev.yml",
        "docker-compose.prod.yml",
        "docker-compose.all.yml",
    ):
        assert not (API_COMPOSE.parent / obsolete_name).exists()


def test_api_secret_configuration_stays_in_an_ignored_environment_file() -> None:
    compose = API_COMPOSE.read_text(encoding="utf-8")
    example = (ROOT / "env-example/.env.api.example").read_text(encoding="utf-8")

    assert "path: ${HARBORRAG_API_ENV_FILE:-../../env/.env.api}" in compose
    assert "CONFLUENCE_TOKEN" not in compose
    assert "JIRA_TOKEN" not in compose
    assert "HARBORRAG_AUTH_MODE=none" in example
    assert "HARBORRAG_ALLOW_INSECURE_DEV=true" in example
    assert "HARBORRAG_API_BIND_ADDRESS=127.0.0.1" in example
    assert "# HARBORRAG_AUTH_SECRET=" in example
    assert "HARBORRAG_AUTH_SECRET=REPLACE" not in example


def test_down_subcommand_stops_composed_projects_in_reverse_order() -> None:
    script = DEV_SCRIPT.read_text(encoding="utf-8")
    down_function = script.split("stop_stack() {", 1)[1].split('command="${1:-}"', 1)[0]

    api_position = down_function.index("api_compose")
    temporal_position = down_function.index("temporal_compose")
    database_position = down_function.index("data_compose")

    assert api_position < temporal_position < database_position
