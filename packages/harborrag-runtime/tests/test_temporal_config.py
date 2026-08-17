from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from harborrag_runtime.config import (
    TEMPORAL_CONFIG_VERSION,
    TemporalConfigurationError,
    TemporalConnectionConfig,
    TemporalRuntimeConfig,
    load_temporal_config,
)
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.errors import RuntimeConfigurationError
from harborrag_runtime.temporal.schemas import SourceIngestionInput

ROOT = Path(__file__).resolve().parents[3]


def test_tracked_temporal_configuration_loads_all_runtime_sections() -> None:
    config = load_temporal_config(ROOT / "config/temporal.yaml")

    assert TEMPORAL_CONFIG_VERSION == 1
    assert config.connection.target == "localhost:7233"
    assert config.connection.namespace == "harborrag"
    assert config.connection.tls.enabled is False
    assert config.worker.max_concurrent_activities == 2
    assert config.task_queues.as_tuple() == (
        "harborrag-discovery",
        "harborrag-transform",
        "harborrag-io",
        "harborrag-parser",
        "harborrag-model",
        "harborrag-index",
    )
    assert config.retries.discovery.maximum_attempts == 8
    assert config.retries.document.maximum_attempts == 5
    assert config.workflow_execution_timeout_seconds == 2_592_000
    assert config.health_timeout_seconds == 5


def test_annotated_temporal_example_matches_active_defaults() -> None:
    active = load_temporal_config(ROOT / "config/temporal.yaml")
    example = load_temporal_config(ROOT / "config/temporal.example.yaml")

    assert example == active


def test_temporal_configuration_is_strict_and_versioned(tmp_path: Path) -> None:
    config_path = tmp_path / "temporal.yaml"
    config_path.write_text("version: 1\nunknown: true\n", encoding="utf-8")

    with pytest.raises(TemporalConfigurationError, match="unknown field"):
        load_temporal_config(config_path)

    config_path.write_text("version: 2\n", encoding="utf-8")
    with pytest.raises(TemporalConfigurationError, match="expected 1"):
        load_temporal_config(config_path)

    config_path.write_text(
        "version: 1\nconnection:\n  tls:\n    certificate: tracked-secret\n",
        encoding="utf-8",
    )
    with pytest.raises(TemporalConfigurationError, match="unknown field"):
        load_temporal_config(config_path)


@pytest.mark.parametrize(
    ("yaml_value", "message"),
    [
        (".nan", "must be finite"),
        ('" harborrag-discovery "', "without outer whitespace"),
    ],
)
def test_temporal_configuration_rejects_unsafe_scalars(
    tmp_path: Path,
    yaml_value: str,
    message: str,
) -> None:
    config_path = tmp_path / "temporal.yaml"
    section = (
        f"retries:\n  discovery:\n    initial_interval_seconds: {yaml_value}\n"
        if yaml_value == ".nan"
        else f"task_queues:\n  discovery: {yaml_value}\n"
    )
    config_path.write_text(f"version: 1\n{section}", encoding="utf-8")

    with pytest.raises(TemporalConfigurationError, match=message):
        load_temporal_config(config_path)


def test_runtime_settings_load_yaml_with_explicit_environment_overrides(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "temporal.yaml"
    config_path.write_text(
        """\
version: 1
connection:
  target: yaml.example:7233
  namespace: yaml-namespace
  allow_insecure_remote: true
worker:
  identity: yaml-worker
  max_concurrent_activities: 3
task_queues:
  discovery: test-discovery
  transform: test-transform
  io: test-io
  parser: test-parser
  model: test-model
  index: test-index
retries:
  discovery:
    initial_interval_seconds: 0.1
    maximum_attempts: 3
  document:
    maximum_attempts: 2
workflow:
  execution_timeout_seconds: 600
  task_timeout_seconds: 8
health:
  timeout_seconds: 2.5
""",
        encoding="utf-8",
    )
    settings = RuntimeSettings(
        temporal_config_path=config_path,
        temporal_target="override.example:7233",
        temporal_allow_insecure_remote=True,
        control_db_pool_size=18,
        control_db_max_overflow=0,
    )

    config = TemporalRuntimeConfig.from_settings(settings)

    assert config.connection.target == "override.example:7233"
    assert config.connection.namespace == "yaml-namespace"
    assert config.worker.identity == "yaml-worker"
    assert config.worker.max_concurrent_activities == 3
    assert config.task_queues.discovery == "test-discovery"
    assert config.task_queues.index == "test-index"
    assert config.retries.discovery.initial_interval_seconds == 0.1
    assert config.retries.discovery.maximum_attempts == 3
    assert config.retries.document.maximum_attempts == 2
    assert config.workflow_execution_timeout_seconds == 600
    assert config.workflow_task_timeout_seconds == 8
    assert config.health_timeout_seconds == 2.5


def test_explicit_missing_temporal_configuration_fails(tmp_path: Path) -> None:
    settings = RuntimeSettings(temporal_config_path=tmp_path / "missing.yaml")

    with pytest.raises(TemporalConfigurationError, match="does not exist"):
        TemporalRuntimeConfig.from_settings(settings)


def test_tls_environment_override_preserves_yaml_domain(tmp_path: Path) -> None:
    config_path = tmp_path / "temporal.yaml"
    config_path.write_text(
        """\
version: 1
connection:
  target: temporal.example:7233
  tls:
    enabled: true
    domain: temporal.internal
""",
        encoding="utf-8",
    )
    settings = RuntimeSettings(
        temporal_config_path=config_path,
        temporal_tls=True,
        temporal_api_key="secret-token",
    )

    config = TemporalRuntimeConfig.from_settings(settings)

    assert config.connection.tls.domain == "temporal.internal"
    assert config.connection.api_key == "secret-token"
    assert "secret-token" not in repr(config)


def test_empty_temporal_api_key_is_normalized_to_none() -> None:
    settings = RuntimeSettings(temporal_api_key="")

    assert TemporalRuntimeConfig.from_settings(settings).connection.api_key is None


def test_temporal_api_key_requires_tls() -> None:
    settings = RuntimeSettings(temporal_api_key="secret-token")

    with pytest.raises(RuntimeConfigurationError, match="api_key requires TLS"):
        TemporalRuntimeConfig.from_settings(settings)


@pytest.mark.parametrize(
    "target",
    (
        "http://temporal:7233",
        "temporal:7233/path",
        "temporal",
        "temporal:not-a-port",
        "temporal:0",
        "temporal:99999",
        "user@temporal:7233",
        "temporal:7233?query=true",
        "[broken:7233",
    ),
)
def test_temporal_target_requires_plain_host_port_authority(target: str) -> None:
    with pytest.raises(RuntimeConfigurationError, match="host:port"):
        TemporalConnectionConfig(target=target, allow_insecure_remote=True)


def test_temporal_target_accepts_bracketed_loopback_ipv6() -> None:
    config = TemporalConnectionConfig(target="[::1]:7233")

    assert config.target == "[::1]:7233"


def test_temporal_yaml_rejects_duplicate_keys(tmp_path: Path) -> None:
    config_path = tmp_path / "temporal.yaml"
    config_path.write_text(
        """version: 1
connection:
  target: localhost:7233
  target: temporal:7233
""",
        encoding="utf-8",
    )

    with pytest.raises(TemporalConfigurationError, match="duplicate key 'target'"):
        load_temporal_config(config_path)


def test_workflow_options_preserve_existing_positional_input_order() -> None:
    names = tuple(field.name for field in fields(SourceIngestionInput))

    assert names[-2:] == ("continuation", "workflow_options")
