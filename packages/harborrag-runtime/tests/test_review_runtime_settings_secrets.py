from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from harborrag_runtime.composition.resources import build_graph_config, build_vector_repository
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.errors import RuntimeConfigurationError

pytestmark = pytest.mark.unit

_SECRET = "Tail-7f3a"
_SECRET_TAIL = _SECRET[-4:]
_PRODUCTION_DB = "postgresql+asyncpg://harbor@db.example.internal/harbor"
_REMOTE_TEMPORAL = "temporal.example.internal:7233"


def _production(**overrides: object) -> RuntimeSettings:
    values: dict[str, object] = {"env": "prod", "control_db_url": _PRODUCTION_DB, **overrides}
    return RuntimeSettings(**values)  # type: ignore[arg-type]


def test_failed_model_validator_keeps_secret_values_out_of_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARBORRAG_INGESTION_TENANT_ID", "ACME")
    monkeypatch.setenv("HARBORRAG_OBJECT_STORE_SECRET_ACCESS_KEY", _SECRET)

    with pytest.raises(ValidationError) as caught:
        RuntimeSettings()

    message = str(caught.value)
    assert "HARBORRAG_TENANT_ID" in message
    assert "input_value" not in message
    assert _SECRET_TAIL not in message


def test_failed_after_validator_keeps_secret_values_out_of_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARBORRAG_OBJECT_STORE_SECRET_ACCESS_KEY", _SECRET)

    with pytest.raises(ValidationError) as caught:
        RuntimeSettings(env="prod")

    message = str(caught.value)
    assert "SQLite is development-only" in message
    assert "input_value" not in message
    assert _SECRET_TAIL not in message


def test_qdrant_insecure_remote_switch_is_ignored_outside_dev() -> None:
    settings = _production(
        qdrant_url="http://qdrant.example.internal:6333", qdrant_allow_insecure_remote=True
    )

    with pytest.raises(ValidationError, match="remote Qdrant requires HTTPS"):
        build_vector_repository(settings)


@pytest.mark.parametrize(("env", "allowed"), [("dev", True), ("staging", False), ("prod", False)])
def test_falkordb_insecure_remote_switch_applies_only_in_dev(env: str, allowed: bool) -> None:
    settings = _production(
        env=env, falkordb_host="falkordb.example.internal", falkordb_allow_insecure_remote=True
    )

    if allowed:
        assert build_graph_config(settings).allow_insecure_remote is True
    else:
        with pytest.raises(ValidationError, match="remote FalkorDB requires SSL"):
            build_graph_config(settings)


def _temporal_file(
    tmp_path: Path, *, allow_insecure_remote: bool, target: str = _REMOTE_TEMPORAL
) -> Path:
    path = tmp_path / "temporal.yaml"
    path.write_text(
        "version: 1\n"
        "connection:\n"
        f"  target: {target}\n"
        f"  allow_insecure_remote: {str(allow_insecure_remote).lower()}\n",
        encoding="utf-8",
    )
    return path


def test_temporal_insecure_remote_switch_from_the_environment_is_ignored_outside_dev(
    tmp_path: Path,
) -> None:
    settings = _production(
        temporal_config_path=_temporal_file(
            tmp_path, allow_insecure_remote=False, target="localhost:7233"
        ),
        temporal_target=_REMOTE_TEMPORAL,
        temporal_allow_insecure_remote=True,
    )

    with pytest.raises(RuntimeConfigurationError, match="remote Temporal requires TLS"):
        TemporalRuntimeConfig.from_settings(settings)


def test_temporal_insecure_remote_switch_from_the_file_is_ignored_outside_dev(
    tmp_path: Path,
) -> None:
    settings = _production(
        temporal_config_path=_temporal_file(tmp_path, allow_insecure_remote=True)
    )

    with pytest.raises(RuntimeConfigurationError, match="remote Temporal requires TLS"):
        TemporalRuntimeConfig.from_settings(settings)


def test_temporal_insecure_remote_switch_still_applies_in_dev(tmp_path: Path) -> None:
    settings = RuntimeSettings(
        env="dev",
        temporal_config_path=_temporal_file(tmp_path, allow_insecure_remote=True),
    )

    config = TemporalRuntimeConfig.from_settings(settings)

    assert config.connection.allow_insecure_remote is True
