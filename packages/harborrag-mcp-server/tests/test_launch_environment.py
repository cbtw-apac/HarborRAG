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
        "MINIO_ROOT_USER=object-root\nMINIO_ROOT_PASSWORD=object-root-secret\n"
        "MINIO_APP_SECRET_KEY=object-app-secret\n"
        "QDRANT_HTTP_PORT=7333\nQDRANT_API_KEY=qdrant-synthetic\n"
        "FALKORDB_PASSWORD=falkor-synthetic\n",
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
    assert environment["HARBORRAG_OBJECT_STORE_ACCESS_KEY_ID"] == "harborrag-app"
    assert environment["HARBORRAG_MCP_CONFIG_PATH"] == str(tmp_path / "config/custom-mcp.yaml")
    assert environment["HARBORRAG_MODEL_CONFIG_PATH"] == str(tmp_path / "config/models.yaml")


def test_checkout_environment_maps_qdrant_and_falkordb_credentials(tmp_path: Path) -> None:
    _checkout(tmp_path)
    environment: dict[str, str] = {}

    load_launch_environment(checkout_root=tmp_path, environ=environment)

    assert environment["HARBORRAG_QDRANT_API_KEY"] == "qdrant-synthetic"
    assert environment["HARBORRAG_FALKORDB_PASSWORD"] == "falkor-synthetic"


def test_explicit_qdrant_and_falkordb_credentials_win_over_checkout(tmp_path: Path) -> None:
    _checkout(tmp_path)
    environment = {
        "HARBORRAG_QDRANT_API_KEY": "explicit-q",
        "HARBORRAG_FALKORDB_PASSWORD": "explicit-f",
    }

    load_launch_environment(checkout_root=tmp_path, environ=environment)

    assert environment["HARBORRAG_QDRANT_API_KEY"] == "explicit-q"
    assert environment["HARBORRAG_FALKORDB_PASSWORD"] == "explicit-f"


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
    assert len(json.loads(result.stdout)) == 11


def test_audit_path_from_env_file_reaches_the_shared_audit_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import harborrag_mcp_server.server.server as server_module

    audit_path = tmp_path / "from-env-file" / "audit.jsonl"
    env_file = tmp_path / "mcp.env"
    env_file.write_text(f"HARBORRAG_MCP_AUDIT_PATH={audit_path}\n", encoding="utf-8")
    monkeypatch.delenv("HARBORRAG_MCP_AUDIT_PATH")
    # The CLI has already imported the server module by the time env files load.
    monkeypatch.setattr(server_module, "_default_audit_log", None)

    load_launch_environment(checkout_root=None, env_files=[env_file])

    assert server_module.McpServer().audit.path == audit_path


def test_checkout_environment_never_hands_mcp_the_minio_root_credentials(
    tmp_path: Path,
) -> None:
    _checkout(tmp_path)
    environment: dict[str, str] = {}

    load_launch_environment(checkout_root=tmp_path, environ=environment)

    assert environment["HARBORRAG_OBJECT_STORE_ACCESS_KEY_ID"] == "harborrag-app"
    assert environment["HARBORRAG_OBJECT_STORE_SECRET_ACCESS_KEY"] == "object-app-secret"
    assert "object-root" not in environment.values()
    assert "object-root-secret" not in environment.values()


def test_checkout_environment_drops_every_raw_checkout_only_secret(tmp_path: Path) -> None:
    _checkout(tmp_path)
    database = tmp_path / "env/.env.database"
    database.write_text(
        database.read_text(encoding="utf-8")
        + "REDIS_PASSWORD=sentinel-redis\nHARBORRAG_SECRETS_ENCRYPTION_KEY=sentinel-fernet\n",
        encoding="utf-8",
    )
    environment: dict[str, str] = {}

    load_launch_environment(checkout_root=tmp_path, environ=environment)

    for name in (
        "MINIO_ROOT_USER",
        "MINIO_ROOT_PASSWORD",
        "MINIO_APP_SECRET_KEY",
        "POSTGRES_PASSWORD",
        "REDIS_PASSWORD",
        "HARBORRAG_SECRETS_ENCRYPTION_KEY",
    ):
        assert name not in environment
    for sentinel in ("object-root-secret", "sentinel-redis", "sentinel-fernet", "p@ss:/word"):
        assert sentinel not in environment.values()
    assert environment["HARBORRAG_OBJECT_STORE_SECRET_ACCESS_KEY"] == "object-app-secret"


def test_explicitly_set_checkout_secret_names_are_kept(tmp_path: Path) -> None:
    _checkout(tmp_path)
    environment = {"REDIS_PASSWORD": "caller-set"}

    load_launch_environment(checkout_root=tmp_path, environ=environment)

    assert environment["REDIS_PASSWORD"] == "caller-set"


@pytest.mark.parametrize("check", [True, False])
def test_missing_application_secret_key_is_reported_by_name(tmp_path: Path, check: bool) -> None:
    _checkout(tmp_path)
    database = tmp_path / "env/.env.database"
    database.write_text(
        database.read_text(encoding="utf-8").replace(
            "MINIO_APP_SECRET_KEY=object-app-secret\n", ""
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="MINIO_APP_SECRET_KEY"):
        load_launch_environment(checkout_root=tmp_path, environ={}, check=check)


def test_checkout_environment_honours_a_custom_application_access_key(tmp_path: Path) -> None:
    _checkout(tmp_path)
    environment = {"MINIO_APP_ACCESS_KEY": "custom-app"}

    load_launch_environment(checkout_root=tmp_path, environ=environment)

    assert environment["HARBORRAG_OBJECT_STORE_ACCESS_KEY_ID"] == "custom-app"


def test_explicit_object_store_settings_win_over_the_application_key(tmp_path: Path) -> None:
    _checkout(tmp_path)
    environment = {
        "HARBORRAG_OBJECT_STORE_ACCESS_KEY_ID": "explicit-id",
        "HARBORRAG_OBJECT_STORE_SECRET_ACCESS_KEY": "explicit-secret",
    }

    load_launch_environment(checkout_root=tmp_path, environ=environment)

    assert environment["HARBORRAG_OBJECT_STORE_ACCESS_KEY_ID"] == "explicit-id"
    assert environment["HARBORRAG_OBJECT_STORE_SECRET_ACCESS_KEY"] == "explicit-secret"
