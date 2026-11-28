"""Regression checks for the supported local development topology."""

from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from harborrag_runtime.config.connectors.importing import ConnectionImportService

ROOT = Path(__file__).resolve().parents[1]
API_COMPOSE = ROOT / "deploy/compose/docker-compose.yml"
DEV_SCRIPT = ROOT / "scripts/deployment/dev.sh"
CONNECTORS = ROOT / "config/sources/connectors.yaml"


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

    assert monitoring.count("HARBORRAG_MONITORING_BIND_ADDRESS:-127.0.0.1") == 3
    assert "GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD:?" in monitoring
    assert 'GF_AUTH_ANONYMOUS_ENABLED: "false"' in monitoring
    assert 'GF_USERS_ALLOW_SIGN_UP: "false"' in monitoring
    assert 'expose:\n      - "3100"' in monitoring
    assert '"3100:3100"' not in monitoring
    for image in (
        "prom/prometheus:v3.13.1",
        "grafana/grafana:13.1.0",
        "grafana/loki:3.7.2",
        "grafana/tempo:2.8.2",
        "otel/opentelemetry-collector-contrib:0.165.0",
        "prom/node-exporter:v1.13.1",
        "ghcr.io/google/cadvisor:0.57.0",
        "prometheuscommunity/postgres-exporter:v0.21.1",
        "prom/alertmanager:v0.28.1",
    ):
        assert f"image: {image}" in monitoring
    assert "@sha256:" not in monitoring
    assert ":latest" not in monitoring
    assert "internal: true" in monitoring
    assert "grafana_secure_data:/var/lib/grafana" in monitoring
    assert "grafana_data:/var/lib/grafana" not in monitoring


def test_prometheus_reads_the_scrape_token_from_the_monitoring_environment() -> None:
    monitoring = yaml.safe_load(
        (ROOT / "deploy/compose/docker-compose.monitoring.yml").read_text(encoding="utf-8")
    )
    prometheus_config = yaml.safe_load(
        (ROOT / "deploy/prometheus/prometheus.yml").read_text(encoding="utf-8")
    )
    api_job = next(
        job for job in prometheus_config["scrape_configs"] if job["job_name"] == "harborrag-api"
    )
    example = (ROOT / "env-example/.env.monitoring.example").read_text(encoding="utf-8")

    assert monitoring["secrets"]["api-scrape-token"] == {
        "environment": "HARBORRAG_METRICS_SCRAPE_TOKEN"
    }
    assert monitoring["services"]["prometheus"]["secrets"] == ["api-scrape-token"]
    assert api_job["authorization"]["credentials_file"] == "/run/secrets/api-scrape-token"
    assert "HARBORRAG_METRICS_SCRAPE_TOKEN=\n" in example
    assert "api-scrape-token" not in DEV_SCRIPT.read_text(encoding="utf-8")


def _yaml(path: str) -> dict[str, Any]:
    """Return one YAML file of the repository, parsed."""
    document: dict[str, Any] = yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))
    return document


@pytest.mark.unit
def test_monitoring_publishes_only_the_collector_prometheus_and_grafana() -> None:
    monitoring = _yaml("deploy/compose/docker-compose.monitoring.yml")
    services: dict[str, dict[str, Any]] = monitoring["services"]

    published = {name for name, service in services.items() if service.get("ports")}

    assert published == {"otel-collector", "prometheus", "grafana"}
    assert all(service.get("mem_limit") for service in services.values())
    assert monitoring["networks"]["grafana-ui-network"] is None
    assert [
        name for name, service in services.items() if "grafana-ui-network" in service["networks"]
    ] == ["grafana"]
    assert services["tempo"]["networks"] == ["monitoring-network"]
    assert "harborrag-data-network" in services["otel-collector"]["networks"]
    assert [
        name for name, service in services.items() if "alert-egress-network" in service["networks"]
    ] == ["alertmanager"]
    assert services["alertmanager"]["secrets"] == ["alert-oncall-webhook", "alert-team-webhook"]
    assert monitoring["secrets"]["alert-oncall-webhook"] == {
        "environment": "HARBORRAG_ALERT_ONCALL_WEBHOOK_URL"
    }


@pytest.mark.unit
def test_prometheus_scrapes_the_collector_with_exemplars_and_the_exporters() -> None:
    prometheus = _yaml("deploy/compose/docker-compose.monitoring.yml")["services"]["prometheus"]
    jobs = {
        job["job_name"]: job for job in _yaml("deploy/prometheus/prometheus.yml")["scrape_configs"]
    }

    assert "--storage.tsdb.retention.time=30d" in prometheus["command"]
    assert "--enable-feature=exemplar-storage" in prometheus["command"]
    assert set(jobs) == {
        "harborrag-ingestion-worker",
        "harborrag-api",
        "otel-collector",
        "node-exporter",
        "cadvisor",
        "postgres-exporter",
    }
    assert jobs["otel-collector"]["scrape_protocols"][0] == "OpenMetricsText1.0.0"


@pytest.mark.unit
def test_collector_allow_lists_attributes_before_span_metrics_and_sampling() -> None:
    collector = _yaml("deploy/otel-collector/config.yaml")
    transform = collector["processors"]["transform/allowlist"]
    pipelines = collector["service"]["pipelines"]
    policies = {
        policy["type"]: policy for policy in collector["processors"]["tail_sampling"]["policies"]
    }

    statements = {
        group["context"]: " ".join(group["statements"])
        for group in (*transform["trace_statements"], *transform["log_statements"])
    }
    assert {context: "keep_matching_keys(" in text for context, text in statements.items()} == {
        "span": True,
        "spanevent": True,
        "log": True,
    }
    assert statements["log"].count("replace_pattern(log.body") == 3
    assert pipelines["traces"]["processors"][-1] == "transform/allowlist"
    assert pipelines["traces"]["exporters"] == ["span_metrics", "forward/sampling"]
    assert pipelines["traces/sampled"]["processors"][0] == "tail_sampling"
    assert pipelines["logs"]["processors"].index("transform/allowlist") == 1
    assert policies["status_code"]["status_code"]["status_codes"] == ["ERROR"]
    assert policies["latency"]["latency"]["threshold_ms"] == 2000
    assert policies["probabilistic"]["probabilistic"]["sampling_percentage"] == (
        "${env:HARBORRAG_TRACE_SAMPLE_PERCENT}"
    )
    assert collector["exporters"]["otlp_http/tempo"]["endpoint"] == "http://tempo:4318"
    assert collector["exporters"]["otlp_http/loki"]["endpoint"] == "http://loki:3100/otlp"
    assert collector["exporters"]["prometheus"]["enable_open_metrics"] is True


@pytest.mark.unit
def test_grafana_data_sources_have_fixed_uids_and_link_to_each_other() -> None:
    sources = {
        source["uid"]: source
        for source in _yaml("deploy/grafana/provisioning/datasources/datasources.yml")[
            "datasources"
        ]
    }

    assert set(sources) == {
        "harborrag-prometheus",
        "harborrag-loki",
        "harborrag-tempo",
    }
    assert [uid for uid, source in sources.items() if source.get("isDefault")] == [
        "harborrag-prometheus"
    ]
    tempo = sources["harborrag-tempo"]["jsonData"]
    assert tempo["tracesToLogsV2"]["datasourceUid"] == "harborrag-loki"
    assert tempo["serviceMap"]["datasourceUid"] == "harborrag-prometheus"
    assert sources["harborrag-loki"]["jsonData"]["derivedFields"][0]["datasourceUid"] == (
        "harborrag-tempo"
    )


@pytest.mark.unit
def test_grafana_deletes_the_retired_postgres_data_source_and_leaves_the_data_network() -> None:
    provisioning = _yaml("deploy/grafana/provisioning/datasources/datasources.yml")
    grafana = _yaml("deploy/compose/docker-compose.monitoring.yml")["services"]["grafana"]

    assert provisioning["deleteDatasources"] == [{"name": "PostgreSQL", "orgId": 1}]
    assert grafana["networks"] == ["monitoring-network", "grafana-ui-network"]
    assert not [name for name in grafana["environment"] if "OBSERVER" in name]
    assert not (ROOT / "deploy/compose/postgres/observer-role.sql").exists()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("compose_file", "service", "env_file"),
    [
        ("deploy/compose/docker-compose.yml", "api", "${HARBORRAG_API_ENV_FILE:-"),
        ("deploy/compose/docker-compose.mcp.yml", "mcp", "${HARBORRAG_MCP_ENV_FILE:-"),
    ],
)
def test_api_and_mcp_containers_receive_their_env_file(
    compose_file: str, service: str, env_file: str
) -> None:
    paths = [entry["path"] for entry in _service(compose_file, service)["env_file"]]

    assert any(path.startswith(env_file) for path in paths)


@pytest.mark.unit
def test_the_worker_receives_the_collector_endpoint_only_when_its_env_file_sets_it() -> None:
    environment = _service("deploy/compose/docker-compose.temporal.yml", "temporal-worker")[
        "environment"
    ]

    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in environment
    assert environment["OTEL_EXPORTER_OTLP_ENDPOINT"] is None
    for name in (".env.api.example", ".env.temporal.example", ".env.mcp.example"):
        template = (ROOT / "env-example" / name).read_text(encoding="utf-8")
        assert "# OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318\n" in template, name


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


def test_api_and_worker_mount_the_same_config_directory() -> None:
    api = API_COMPOSE.read_text(encoding="utf-8")
    temporal = (ROOT / "deploy/compose/docker-compose.temporal.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "deploy/docker/Dockerfile.temporal-worker").read_text(encoding="utf-8")

    assert "../../config:/app/config:ro" in api
    assert "../../config:/app/config:ro" in temporal
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
        "chunking,chunking-code,control-plane,falkordb,langfuse,llm,opentelemetry,parsers,"
        "pdf-liteparse,postgres,qdrant,redis,s3,tables"
    )
    release_version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["version"]
    assert f"harborrag-adapters[{adapter_extras}]=={release_version}" in runtime_project
    assert f"harborrag-adapters[{adapter_extras}]'" in dockerfile


def test_mcp_compose_reaches_fixed_backends_on_the_data_network() -> None:
    """Pin the backend addresses as literals.

    The plaintext acknowledgements beside them then cannot be pointed at a remote
    host by an override.
    """
    mcp = (ROOT / "deploy/compose/docker-compose.mcp.yml").read_text(encoding="utf-8")

    assert "network_mode" not in mcp
    assert "name: harborrag-data-network" in mcp
    assert "@postgres:5432/" in mcp
    for fixed_address in (
        "HARBORRAG_OBJECT_STORE_ENDPOINT_URL: http://minio:9000",
        "HARBORRAG_QDRANT_URL: http://qdrant:6333",
        "HARBORRAG_FALKORDB_HOST: falkordb",
    ):
        assert fixed_address in mcp


def test_mcp_compose_publishes_its_port_on_loopback_only() -> None:
    mcp = (ROOT / "deploy/compose/docker-compose.mcp.yml").read_text(encoding="utf-8")

    assert '- "127.0.0.1:${HARBORRAG_MCP_PORT:-8010}:8010"' in mcp
    assert "HARBORRAG_MCP_HOST: 0.0.0.0" in mcp
    assert 'HARBORRAG_MCP_ALLOWED_HOSTS: "127.0.0.1,localhost"' in mcp
    assert 'HARBORRAG_MCP_ALLOW_INSECURE_BIND: "true"' in mcp


def test_mcp_compose_does_not_receive_the_secrets_encryption_key() -> None:
    # The reader never opens the control-plane secret store; the API and worker
    # files keep requiring the key.
    mcp = (ROOT / "deploy/compose/docker-compose.mcp.yml").read_text(encoding="utf-8")
    api = API_COMPOSE.read_text(encoding="utf-8")

    assert "HARBORRAG_SECRETS_ENCRYPTION_KEY: ${" not in mcp
    assert "HARBORRAG_SECRETS_ENCRYPTION_KEY: ${HARBORRAG_SECRETS_ENCRYPTION_KEY:?" in api


def _service(path: str, name: str) -> dict[str, Any]:
    """Return one service of a Compose file, parsed."""
    document: dict[str, Any] = yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))
    service: dict[str, Any] = document["services"][name]
    return service


def test_the_api_reads_the_same_local_sources_as_the_workers() -> None:
    """Connection tests run in the API, so it must see the folder runs read, read-only."""
    api = _service("deploy/compose/docker-compose.yml", "api")
    worker = _service("deploy/compose/docker-compose.temporal.yml", "temporal-worker")

    def source_mounts(service: dict[str, Any]) -> list[str]:
        return [
            str(volume)
            for volume in service.get("volumes") or []
            if ":/data/sources" in str(volume)
        ]

    api_mounts, worker_mounts = source_mounts(api), source_mounts(worker)
    assert api_mounts == worker_mounts
    assert len(api_mounts) == 1 and api_mounts[0].endswith(":/data/sources:ro")
    assert api["environment"]["LOCAL_SOURCE_PATH"] == worker["environment"]["LOCAL_SOURCE_PATH"]


class _FreshInstallStore:
    """Project and connection store of an install that holds nothing yet."""

    async def get(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def get_by_source_key(self, *_args: object, **_kwargs: object) -> None:
        return None


def test_the_api_has_every_variable_the_connector_import_reads() -> None:
    """The first-start import runs in the API: it reads plain values there, secrets stay out."""
    environment = set(_service("deploy/compose/docker-compose.yml", "api")["environment"])
    importer = ConnectionImportService(
        projects=_FreshInstallStore(),
        connections=_FreshInstallStore(),
        environ=dict.fromkeys(environment, "set"),
    )

    plan = asyncio.run(
        importer.plan(
            CONNECTORS.read_text(encoding="utf-8"),
            tenant_id="DEFAULT",
            base_directory=CONNECTORS.parent,
        )
    )

    unset = [
        f"{change.connection_id}: {issue}"
        for change in plan.changes
        for issue in (change.reason or "").split("; ")
        if issue.endswith("is not set")
    ]
    referenced = {
        reference.removeprefix("env:")
        for change in plan.changes
        if change.connection is not None
        for reference in change.connection.secret_refs
    }
    assert unset == []
    assert referenced.isdisjoint(environment)


def test_mcp_compose_brings_the_server_back_after_docker_restarts() -> None:
    mcp = (ROOT / "deploy/compose/docker-compose.mcp.yml").read_text(encoding="utf-8")

    assert "restart: unless-stopped" in mcp
    assert "on-failure" not in mcp


def test_mcp_images_record_the_commit_they_were_built_from() -> None:
    mcp = (ROOT / "deploy/compose/docker-compose.mcp.yml").read_text(encoding="utf-8")

    assert "HARBORRAG_GIT_REVISION: ${HARBORRAG_GIT_REVISION:-unknown}" in mcp
    for name in ("Dockerfile.mcp", "Dockerfile.mcp-ui"):
        dockerfile = (ROOT / "deploy/docker" / name).read_text(encoding="utf-8")
        assert "ARG HARBORRAG_GIT_REVISION=unknown" in dockerfile
        assert 'org.opencontainers.image.revision="${HARBORRAG_GIT_REVISION}"' in dockerfile


def test_shipped_templates_share_the_corpus_mode_of_the_default_tenant() -> None:
    expected = {
        "HARBORRAG_CORPUS_ACCESS_MODE": "tenant_shared",
        "HARBORRAG_CORPUS_SHARED_TENANT_ID": "DEFAULT",
    }

    for name in (".env.api.example", ".env.temporal.example", ".env.mcp.example"):
        lines = (ROOT / "env-example" / name).read_text(encoding="utf-8").splitlines()
        active = dict(line.split("=", 1) for line in lines if line.startswith("HARBORRAG_CORPUS_"))
        assert active == expected, name
