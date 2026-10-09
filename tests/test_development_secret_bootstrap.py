"""dev.sh generates local secrets, never rotates them, and refuses placeholders."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEV_SCRIPT = ROOT / "scripts/deployment/dev.sh"
DATABASE_SECRETS = (
    "POSTGRES_PASSWORD",
    "MINIO_ROOT_PASSWORD",
    "QDRANT_API_KEY",
    "REDIS_PASSWORD",
    "FALKORDB_PASSWORD",
)
HEX_SECRET = re.compile(r"^[0-9a-f]{64}$")

requires_openssl = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="dev.sh generates secrets with openssl"
)


def _project(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    project = tmp_path / "project"
    script = project / "scripts/deployment/dev.sh"
    script.parent.mkdir(parents=True)
    script.write_text(DEV_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    shutil.copytree(ROOT / "env-example", project / "env-example")
    (project / "docs").mkdir()
    fake_bin = project / "fake-bin"
    fake_bin.mkdir()
    docker_log = project / "docker.log"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/usr/bin/env sh
printf '%s\\n' "$*" >> "$DEV_DOCKER_LOG"
case " $* " in
  *" config --services "*) printf 'api\\n' ;;
esac
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    env = {
        **os.environ,
        "DEV_DOCKER_LOG": str(docker_log),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }
    # dev.sh lets an exported value win over the file, exactly as Compose does.
    for name in (*DATABASE_SECRETS, "HARBORRAG_SECRETS_ENCRYPTION_KEY"):
        env.pop(name, None)
    return script, docker_log, env


def _run(script: Path, env: dict[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *arguments],
        cwd=script.parents[2],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
    )


def _values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            name, _, value = line.partition("=")
            values[name] = value
    return values


def _ready_project(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    """A bootstrapped checkout with the one value bootstrap leaves to the operator."""

    script, docker_log, env = _project(tmp_path)
    assert _run(script, env, "bootstrap").returncode == 0
    database = script.parents[2] / "env/.env.database"
    database.write_text(
        database.read_text(encoding="utf-8").replace(
            "HARBORRAG_SECRETS_ENCRYPTION_KEY=\n",
            "HARBORRAG_SECRETS_ENCRYPTION_KEY=test-encryption-key\n",
        ),
        encoding="utf-8",
    )
    return script, docker_log, env


@requires_openssl
def test_bootstrap_generates_every_database_secret(tmp_path: Path) -> None:
    script, _, env = _project(tmp_path)

    result = _run(script, env, "bootstrap")

    assert result.returncode == 0, result.stderr
    database = script.parents[2] / "env/.env.database"
    values = _values(database)
    for name in DATABASE_SECRETS:
        assert HEX_SECRET.match(values[name]), name
    assert len({values[name] for name in DATABASE_SECRETS}) == len(DATABASE_SECRETS)
    assert "change-me" not in database.read_text(encoding="utf-8")
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    assert "Generated" in result.stdout


@requires_openssl
def test_bootstrap_never_rotates_existing_secrets(tmp_path: Path) -> None:
    script, _, env = _project(tmp_path)
    assert _run(script, env, "bootstrap").returncode == 0
    database = script.parents[2] / "env/.env.database"
    first = database.read_text(encoding="utf-8")

    result = _run(script, env, "bootstrap")

    assert result.returncode == 0, result.stderr
    assert database.read_text(encoding="utf-8") == first
    assert "Generated" not in result.stdout


@requires_openssl
def test_bootstrap_appends_missing_secrets_to_an_older_file(tmp_path: Path) -> None:
    script, _, env = _project(tmp_path)
    environment = script.parents[2] / "env"
    environment.mkdir()
    database = environment / ".env.database"
    # An older checkout: no datastore keys and no trailing newline.
    database.write_text(
        "POSTGRES_PASSWORD=kept-postgres-value\n"
        "MINIO_ROOT_PASSWORD=kept-minio-value\n"
        "HARBORRAG_SECRETS_ENCRYPTION_KEY=kept-key",
        encoding="utf-8",
    )

    result = _run(script, env, "bootstrap")

    assert result.returncode == 0, result.stderr
    values = _values(database)
    assert values["POSTGRES_PASSWORD"] == "kept-postgres-value"
    assert values["MINIO_ROOT_PASSWORD"] == "kept-minio-value"
    assert values["HARBORRAG_SECRETS_ENCRYPTION_KEY"] == "kept-key"
    for name in ("QDRANT_API_KEY", "REDIS_PASSWORD", "FALKORDB_PASSWORD"):
        assert HEX_SECRET.match(values[name]), name
    assert database.read_text(encoding="utf-8").endswith("\n")


@requires_openssl
def test_bootstrap_treats_crlf_empty_values_as_missing(tmp_path: Path) -> None:
    script, _, env = _project(tmp_path)
    environment = script.parents[2] / "env"
    environment.mkdir()
    database = environment / ".env.database"
    database.write_bytes(b"POSTGRES_PASSWORD=\r\nMINIO_ROOT_PASSWORD=\r\n")

    result = _run(script, env, "bootstrap")

    assert result.returncode == 0, result.stderr
    values = _values(database)
    assert HEX_SECRET.match(values["POSTGRES_PASSWORD"])
    assert HEX_SECRET.match(values["MINIO_ROOT_PASSWORD"])


@requires_openssl
@pytest.mark.parametrize("command", ["data", "temporal", "up"])
@pytest.mark.parametrize("name", ["POSTGRES_PASSWORD", "MINIO_ROOT_PASSWORD", "QDRANT_API_KEY"])
def test_launchers_refuse_placeholder_secrets_without_calling_docker(
    tmp_path: Path, command: str, name: str
) -> None:
    script, docker_log, env = _ready_project(tmp_path)
    database = script.parents[2] / "env/.env.database"
    values = _values(database)
    database.write_text(
        database.read_text(encoding="utf-8").replace(
            f"{name}={values[name]}\n", f"{name}=change-me\n"
        ),
        encoding="utf-8",
    )

    result = _run(script, env, command)

    assert result.returncode == 2
    assert name in result.stderr
    assert "env/.env.database" in result.stderr
    calls = docker_log.read_text(encoding="utf-8") if docker_log.exists() else ""
    assert "up --detach" not in calls


@requires_openssl
@pytest.mark.parametrize("name", ["POSTGRES_PASSWORD", "REDIS_PASSWORD"])
@pytest.mark.parametrize("value", ["p@ss", "p/ss", "p:ss", "p#ss", "p%ss", "p ss"])
def test_launchers_refuse_url_unsafe_secrets(tmp_path: Path, name: str, value: str) -> None:
    script, docker_log, env = _ready_project(tmp_path)
    database = script.parents[2] / "env/.env.database"
    database.write_text(
        database.read_text(encoding="utf-8") + f"{name}={value}\n", encoding="utf-8"
    )

    result = _run(script, env, "data")

    assert result.returncode == 2
    assert name in result.stderr
    assert "URL" in result.stderr
    assert not docker_log.exists()


@requires_openssl
def test_data_starts_with_generated_secrets(tmp_path: Path) -> None:
    script, docker_log, env = _ready_project(tmp_path)

    result = _run(script, env, "data")

    assert result.returncode == 0, result.stderr
    assert "docker-compose.database.yml up --detach" in docker_log.read_text(encoding="utf-8")


def test_down_never_requires_secrets(tmp_path: Path) -> None:
    script, docker_log, env = _project(tmp_path)
    environment = script.parents[2] / "env"
    environment.mkdir()
    # stop_stack tears the API down only when these exist; api_compose also
    # requires the models file.
    for filename in (".env.database", ".env.temporal", ".env.connector", ".env.models", ".env.api"):
        (environment / filename).write_text("POSTGRES_PASSWORD=change-me\n", encoding="utf-8")

    result = _run(script, env, "down")

    assert result.returncode == 0, result.stderr
    assert "docker-compose.database.yml down" in docker_log.read_text(encoding="utf-8")
