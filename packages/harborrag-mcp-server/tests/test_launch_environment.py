"""The checkout adapter is the only owner of local MCP environment translation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from harborrag_mcp_server.configuration.launch import load_launch_environment


def _checkout(root: Path) -> None:
    (root / "env").mkdir()
    (root / "env/.env.database").write_text(
        "POSTGRES_USER=reader\nPOSTGRES_PASSWORD=p@ss:/word\nPOSTGRES_DB=harbor\n"
        "MINIO_ROOT_USER=object-reader\nMINIO_ROOT_PASSWORD=object-secret\n"
        "QDRANT_HTTP_PORT=7333\n",
        encoding="utf-8",
    )
    (root / "env/.env.models").write_text("HARBOR_EMBED_API_KEY=secret\n", encoding="utf-8")
    (root / "env/.env.mcp").write_text(
        "HARBORRAG_MCP_CONFIG_PATH=config/custom-mcp.yaml\n", encoding="utf-8"
    )


def test_checkout_environment_maps_local_backends_and_respects_process_overrides(
    tmp_path: Path,
) -> None:
    _checkout(tmp_path)
    environment = {"HARBORRAG_QDRANT_URL": "https://remote.example/qdrant"}

    load_launch_environment(checkout_root=tmp_path, environ=environment)

    assert environment["HARBORRAG_CONTROL_DB_URL"] == (
        "postgresql+asyncpg://reader:p%40ss%3A%2Fword@localhost:5432/harbor"
    )
    assert environment["HARBORRAG_QDRANT_URL"] == "https://remote.example/qdrant"
    assert environment["HARBORRAG_OBJECT_STORE_ACCESS_KEY_ID"] == "object-reader"
    assert environment["HARBORRAG_MCP_CONFIG_PATH"] == str(tmp_path / "config/custom-mcp.yaml")
    assert environment["HARBORRAG_MODEL_CONFIG_PATH"] == str(tmp_path / "config/models.yaml")


def test_explicit_environment_file_overrides_checkout_file_but_not_process_env(
    tmp_path: Path,
) -> None:
    _checkout(tmp_path)
    extra = tmp_path / "extra.env"
    extra.write_text("POSTGRES_DB=other\nHARBOR_EMBED_API_KEY=other-key\n", encoding="utf-8")
    environment = {"HARBOR_EMBED_API_KEY": "process-key"}

    load_launch_environment(checkout_root=tmp_path, env_files=[extra], environ=environment)

    assert environment["HARBOR_EMBED_API_KEY"] == "process-key"
    assert environment["HARBORRAG_CONTROL_DB_URL"].endswith("/other")


def test_invalid_env_line_reports_location_without_secret(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.env"
    invalid.write_text("SECRET=hidden\ninvalid statement hidden-secret\n", encoding="utf-8")
    environment: dict[str, str] = {}

    with pytest.raises(ValueError, match="line 2") as error:
        load_launch_environment(checkout_root=None, env_files=[invalid], environ=environment)

    assert "hidden-secret" not in str(error.value)
    assert environment == {}


def test_checkout_requires_database_settings_for_serving_but_not_catalog_check(
    tmp_path: Path,
) -> None:
    (tmp_path / "env").mkdir()
    (tmp_path / "env/.env.database").write_text("POSTGRES_USER=reader\n", encoding="utf-8")
    (tmp_path / "env/.env.models").write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="POSTGRES_PASSWORD, POSTGRES_DB"):
        load_launch_environment(checkout_root=tmp_path, environ={})

    load_launch_environment(checkout_root=tmp_path, check=True, environ={})


def test_installed_command_accepts_env_file_without_checkout(tmp_path: Path) -> None:
    defaults = Path(__file__).resolve().parents[1] / "src/harborrag_mcp_server/defaults/mcp.yaml"
    env_file = tmp_path / "reader.env"
    env_file.write_text(f"HARBORRAG_MCP_CONFIG_PATH={defaults}\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "harborrag_mcp_server", "--check", "--env-file", str(env_file)],
        cwd=tmp_path,
        env={key: value for key, value in os.environ.items() if key != "HARBORRAG_MCP_CONFIG_PATH"},
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)) == 13
