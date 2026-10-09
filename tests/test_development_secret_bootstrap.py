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
    "MINIO_APP_SECRET_KEY",
    "QDRANT_API_KEY",
    "REDIS_PASSWORD",
    "FALKORDB_PASSWORD",
)
COMPOSE_DIR = ROOT / "deploy/compose"
REQUIRED_VARIABLE = re.compile(r"\$\{([A-Z][A-Z0-9_]*):\?")


def _required_variables(*filenames: str) -> list[str]:
    """Every `${VAR:?}` the named Compose files interpolate."""

    found: set[str] = set()
    for filename in filenames:
        found.update(REQUIRED_VARIABLE.findall((COMPOSE_DIR / filename).read_text("utf-8")))
    return sorted(found)


DOWN_COMPOSE_FILES = (
    "docker-compose.database.yml",
    "docker-compose.temporal.yml",
    "docker-compose.yml",
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
# Compose interpolates required variables on `down` too; record what it would see.
case " $* " in
  *" down "*)
    for name in $DEV_DOWN_REQUIRED; do
      eval "value=\\${$name:-}"
      [ -n "$value" ] || printf 'MISSING %s\\n' "$name" >> "$DEV_DOCKER_LOG.down-env"
    done
    printf 'checked\\n' >> "$DEV_DOCKER_LOG.down-env"
    ;;
esac
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    env = {
        **os.environ,
        "DEV_DOCKER_LOG": str(docker_log),
        "DEV_DOWN_REQUIRED": " ".join(_required_variables(*DOWN_COMPOSE_FILES)),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }
    # dev.sh lets an exported value win over the file, exactly as Compose does.
    for name in (*DATABASE_SECRETS, *_required_variables(*DOWN_COMPOSE_FILES)):
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
    for name in DATABASE_SECRETS:
        assert values[name] not in result.stdout + result.stderr, name


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
@pytest.mark.parametrize("name", ["POSTGRES_PASSWORD", "REDIS_PASSWORD", "MINIO_ROOT_PASSWORD"])
@pytest.mark.parametrize(
    "value",
    ["p@ss", "p/ss", "p:ss", "p#ss", "p%ss", "p ss", "p$ss", "p'ss", "p\\ss", "p&ss", "p+ss"],
)
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


def test_down_survives_required_variable_interpolation_on_an_old_env_file(
    tmp_path: Path,
) -> None:
    script, docker_log, env = _project(tmp_path)
    environment = script.parents[2] / "env"
    environment.mkdir()
    # An older checkout: none of the datastore secrets exist, and Compose now
    # aborts `down` on any unset `${VAR:?...}`.
    for filename in (".env.database", ".env.temporal", ".env.connector", ".env.models", ".env.api"):
        (environment / filename).write_text(
            "POSTGRES_USER=harbor\nHARBORRAG_SECRETS_ENCRYPTION_KEY=\n", encoding="utf-8"
        )

    result = _run(script, env, "down")

    assert result.returncode == 0, result.stderr
    seen = docker_log.with_name("docker.log.down-env").read_text(encoding="utf-8")
    assert seen.count("checked") == 3
    assert "MISSING" not in seen
    # The placeholders exist only for the teardown process and are never written.
    assert "POSTGRES_PASSWORD" not in (environment / ".env.database").read_text(encoding="utf-8")


@requires_openssl
def test_bootstrap_keeps_exported_values_and_appends_nothing_for_them(tmp_path: Path) -> None:
    script, _, env = _project(tmp_path)
    environment = script.parents[2] / "env"
    environment.mkdir()
    database = environment / ".env.database"
    original = "export POSTGRES_PASSWORD=keepme\n  export MINIO_ROOT_PASSWORD=keepme-too\n"
    database.write_text(original, encoding="utf-8")

    result = _run(script, env, "bootstrap")

    assert result.returncode == 0, result.stderr
    text = database.read_text(encoding="utf-8")
    assert text.startswith(original)
    assert "\nPOSTGRES_PASSWORD=" not in text
    assert "\nMINIO_ROOT_PASSWORD=" not in text
    assert HEX_SECRET.match(_values(database)["QDRANT_API_KEY"])
    assert stat.S_IMODE(database.stat().st_mode) == 0o600


@requires_openssl
def test_worker_refuses_a_passwordless_redis_override(tmp_path: Path) -> None:
    script, docker_log, env = _ready_project(tmp_path)
    temporal = script.parents[2] / "env/.env.temporal"
    temporal.write_text(
        temporal.read_text(encoding="utf-8") + "HARBORRAG_REDIS_URL=redis://redis:6379/0\n",
        encoding="utf-8",
    )

    result = _run(script, env, "worker")

    assert result.returncode == 2
    assert "HARBORRAG_REDIS_URL" in result.stderr
    assert "env/.env.temporal" in result.stderr
    assert not docker_log.exists()


@requires_openssl
def test_worker_accepts_a_credentialed_redis_override(tmp_path: Path) -> None:
    script, docker_log, env = _ready_project(tmp_path)
    temporal = script.parents[2] / "env/.env.temporal"
    temporal.write_text(
        temporal.read_text(encoding="utf-8")
        + "HARBORRAG_REDIS_URL=rediss://:secret@cache.example.com:6380/0\n",
        encoding="utf-8",
    )

    result = _run(script, env, "worker")

    assert result.returncode == 0, result.stderr
    assert "--profile worker up" in docker_log.read_text(encoding="utf-8")


def _worker_with_redis_url(
    tmp_path: Path, *, file_url: str | None, exported: str | None
) -> tuple[subprocess.CompletedProcess[str], Path]:
    script, docker_log, env = _ready_project(tmp_path)
    if file_url is not None:
        temporal = script.parents[2] / "env/.env.temporal"
        temporal.write_text(
            temporal.read_text(encoding="utf-8") + f"HARBORRAG_REDIS_URL={file_url}\n",
            encoding="utf-8",
        )
    if exported is not None:
        env["HARBORRAG_REDIS_URL"] = exported
    return _run(script, env, "worker"), docker_log


@requires_openssl
def test_worker_refuses_an_exported_passwordless_redis_url(tmp_path: Path) -> None:
    result, docker_log = _worker_with_redis_url(
        tmp_path, file_url=None, exported="redis://redis:6379/0"
    )

    assert result.returncode == 2
    assert "HARBORRAG_REDIS_URL" in result.stderr
    assert not docker_log.exists()


@requires_openssl
def test_exported_redis_url_wins_over_a_stale_file_line(tmp_path: Path) -> None:
    result, docker_log = _worker_with_redis_url(
        tmp_path,
        file_url="redis://redis:6379/0",
        exported="rediss://:secret@cache.example.com:6380/0",
    )

    assert result.returncode == 0, result.stderr
    assert "--profile worker up" in docker_log.read_text(encoding="utf-8")


@requires_openssl
@pytest.mark.parametrize(
    ("url", "accepted"),
    [
        ("redis://user@cache.example.com:6379/0", False),
        ("redis://user:@cache.example.com:6379/0", False),
        ("redis://cache.example.com:6379/0", False),
        ("redis://user:secret@cache.example.com:6379/0", True),
        ("redis://cache.example.com:6379/0?password=secret", True),
    ],
)
def test_worker_requires_a_password_in_the_redis_url(
    tmp_path: Path, url: str, accepted: bool
) -> None:
    result, _ = _worker_with_redis_url(tmp_path, file_url=url, exported=None)

    assert (result.returncode == 0) is accepted, result.stderr


API_SECRET_LINE = re.compile(r"^HARBORRAG_AUTH_SECRET=([0-9a-f]{64})$", re.MULTILINE)


@requires_openssl
def test_bootstrap_ships_the_api_with_hmac_and_a_generated_secret(tmp_path: Path) -> None:
    script, _, env = _project(tmp_path)

    assert _run(script, env, "bootstrap").returncode == 0

    api = (script.parents[2] / "env/.env.api").read_text(encoding="utf-8")
    assert _values(script.parents[2] / "env/.env.api")["HARBORRAG_AUTH_MODE"] == "hmac"
    assert API_SECRET_LINE.search(api)


def _write_api_env(script: Path, content: str) -> None:
    (script.parents[2] / "env/.env.api").write_text(content, encoding="utf-8")


@requires_openssl
@pytest.mark.parametrize(
    "content",
    [
        "HARBORRAG_AUTH_MODE=hmac\nHARBORRAG_AUTH_SECRET=\n",
        "HARBORRAG_AUTH_MODE=hmac\nHARBORRAG_AUTH_SECRET=too-short\n",
        "HARBORRAG_AUTH_MODE=hmac\nHARBORRAG_AUTH_SECRET=REPLACE_WITH_A_LONG_RANDOM_VALUE_000\n",
    ],
)
def test_api_refuses_hmac_without_a_strong_secret(tmp_path: Path, content: str) -> None:
    script, docker_log, env = _ready_project(tmp_path)
    _write_api_env(script, content)

    result = _run(script, env, "api")

    assert result.returncode == 2
    assert "HARBORRAG_AUTH_SECRET" in result.stderr
    assert "docker-compose.yml up" not in (
        docker_log.read_text(encoding="utf-8") if docker_log.exists() else ""
    )


@requires_openssl
def test_api_allows_the_documented_no_auth_opt_in(tmp_path: Path) -> None:
    script, docker_log, env = _ready_project(tmp_path)
    _write_api_env(script, "HARBORRAG_AUTH_MODE=none\nHARBORRAG_ALLOW_INSECURE_DEV=true\n")

    result = _run(script, env, "api")

    assert result.returncode == 0, result.stderr
    assert "API authentication is disabled" in result.stderr
    assert "docker-compose.yml up" in docker_log.read_text(encoding="utf-8")


@requires_openssl
def test_api_requires_an_explicit_auth_mode(tmp_path: Path) -> None:
    script, _, env = _ready_project(tmp_path)
    _write_api_env(script, "HARBORRAG_API_PORT=8000\n")

    result = _run(script, env, "api")

    assert result.returncode == 2
    assert "HARBORRAG_AUTH_MODE" in result.stderr


@requires_openssl
def test_api_starts_with_the_bootstrapped_hmac_configuration(tmp_path: Path) -> None:
    script, docker_log, env = _ready_project(tmp_path)

    result = _run(script, env, "api")

    assert result.returncode == 0, result.stderr
    assert "authentication is disabled" not in result.stderr
    assert "docker-compose.yml up" in docker_log.read_text(encoding="utf-8")


@requires_openssl
@pytest.mark.parametrize("content", ["HARBORRAG_AUTH_MODE=none\n", "HARBORRAG_API_PORT=8000\n"])
def test_token_refuses_up_front_unless_auth_mode_is_hmac(tmp_path: Path, content: str) -> None:
    script, docker_log, env = _ready_project(tmp_path)
    _write_api_env(script, content)

    result = _run(script, env, "token")

    assert result.returncode == 2
    assert "set HARBORRAG_AUTH_MODE=hmac" in result.stderr
    assert not docker_log.exists() or "dev_token" not in docker_log.read_text(encoding="utf-8")


def test_mcp_teardown_placeholders_cover_every_required_compose_variable() -> None:
    launcher = (ROOT / "scripts/deployment/common/mcp-launcher.sh").read_text(encoding="utf-8")
    function = launcher.split("allow_teardown_without_secrets() {", 1)[1].split("\n}", 1)[0]
    for name in _required_variables("docker-compose.mcp.yml"):
        assert name in function, name


@requires_openssl
def test_bootstrap_on_an_existing_checkout_names_each_remaining_problem(tmp_path: Path) -> None:
    script, _, env = _project(tmp_path)
    assert _run(script, env, "bootstrap").returncode == 0
    root = script.parents[2]
    database = root / "env/.env.database"
    database.write_text(
        database.read_text(encoding="utf-8").replace(
            f"POSTGRES_PASSWORD={_values(database)['POSTGRES_PASSWORD']}",
            "POSTGRES_PASSWORD=change-me-placeholder",
        ),
        encoding="utf-8",
    )
    _write_api_env(script, "HARBORRAG_AUTH_MODE=none\n")
    (root / "env/.env.temporal").write_text(
        "HARBORRAG_REDIS_URL=redis://redis.example.com:6379/0\n", encoding="utf-8"
    )

    result = _run(script, env, "bootstrap")

    assert result.returncode == 0, result.stderr
    notices = [line for line in result.stdout.splitlines() if line.startswith("NOTICE:")]
    assert len(notices) == 3, result.stdout
    assert "POSTGRES_PASSWORD still holds a placeholder" in notices[0]
    assert "HARBORRAG_AUTH_MODE=none" in notices[1]
    assert "HARBORRAG_REDIS_URL" in notices[2]
    assert "change-me-placeholder" not in result.stdout + result.stderr
    assert "redis.example.com" not in result.stdout + result.stderr


@requires_openssl
def test_bootstrap_prints_no_notice_for_a_healthy_checkout(tmp_path: Path) -> None:
    script, _, env = _project(tmp_path)

    result = _run(script, env, "bootstrap")

    assert "NOTICE:" not in result.stdout


@requires_openssl
def test_token_subcommand_mints_inside_the_running_api_container(tmp_path: Path) -> None:
    script, docker_log, env = _ready_project(tmp_path)

    result = _run(script, env, "token")

    assert result.returncode == 0, result.stderr
    assert (
        "docker-compose.yml exec -T api python -m harborrag_app.api.auth.dev_token"
        in docker_log.read_text(encoding="utf-8")
    )


@requires_openssl
def test_api_refuses_no_auth_without_the_insecure_acknowledgement(tmp_path: Path) -> None:
    script, docker_log, env = _ready_project(tmp_path)
    _write_api_env(script, "HARBORRAG_AUTH_MODE=none\n")

    result = _run(script, env, "api")

    assert result.returncode == 2
    assert "HARBORRAG_ALLOW_INSECURE_DEV=true" in result.stderr
    assert "docker-compose.yml up" not in (
        docker_log.read_text(encoding="utf-8") if docker_log.exists() else ""
    )


@requires_openssl
def test_api_no_auth_warning_names_the_migration(tmp_path: Path) -> None:
    script, _, env = _ready_project(tmp_path)
    _write_api_env(script, "HARBORRAG_AUTH_MODE=none\nHARBORRAG_ALLOW_INSECURE_DEV=true\n")

    result = _run(script, env, "api")

    assert result.returncode == 0, result.stderr
    assert "set HARBORRAG_AUTH_MODE=hmac" in result.stderr


@requires_openssl
@pytest.mark.parametrize("bind", ["0.0.0.0", "192.0.2.10"])
def test_api_refuses_no_auth_on_a_non_loopback_bind(tmp_path: Path, bind: str) -> None:
    script, _, env = _ready_project(tmp_path)
    _write_api_env(
        script,
        "HARBORRAG_AUTH_MODE=none\nHARBORRAG_ALLOW_INSECURE_DEV=true\n"
        f"HARBORRAG_API_BIND_ADDRESS={bind}\n",
    )

    result = _run(script, env, "api")

    assert result.returncode == 2
    assert "loopback" in result.stderr


@requires_openssl
def test_data_provisions_the_minio_application_key_after_the_stack_starts(
    tmp_path: Path,
) -> None:
    script, docker_log, env = _ready_project(tmp_path)

    result = _run(script, env, "data")

    assert result.returncode == 0, result.stderr
    calls = docker_log.read_text(encoding="utf-8").splitlines()
    started = next(
        index
        for index, call in enumerate(calls)
        if "docker-compose.database.yml up --detach --scale minio-init=0" in call
    )
    provisioned = next(
        index
        for index, call in enumerate(calls)
        if "docker-compose.database.yml run --rm minio-init" in call
    )
    assert started < provisioned


@requires_openssl
def test_bootstrap_appends_the_minio_application_secret_to_an_older_file(
    tmp_path: Path,
) -> None:
    script, _, env = _project(tmp_path)
    environment = script.parents[2] / "env"
    environment.mkdir()
    database = environment / ".env.database"
    # Written before this key existed, with no trailing newline.
    database.write_text("MINIO_ROOT_PASSWORD=kept-minio-value", encoding="utf-8")

    result = _run(script, env, "bootstrap")

    assert result.returncode == 0, result.stderr
    values = _values(database)
    assert values["MINIO_ROOT_PASSWORD"] == "kept-minio-value"
    assert HEX_SECRET.match(values["MINIO_APP_SECRET_KEY"])


DEV_GUARD_WARNING = "HARBORRAG_ENV=dev"


@requires_openssl
def test_up_warns_once_that_the_stack_runs_development_guards(tmp_path: Path) -> None:
    script, _, env = _ready_project(tmp_path)
    env.pop("HARBORRAG_ENV", None)

    result = _run(script, env, "up")

    assert result.returncode == 0, result.stderr
    assert result.stderr.count(DEV_GUARD_WARNING) == 1


@requires_openssl
@pytest.mark.parametrize("command", ["worker", "api"])
def test_each_launcher_warns_about_development_guards(tmp_path: Path, command: str) -> None:
    script, _, env = _ready_project(tmp_path)
    env.pop("HARBORRAG_ENV", None)

    result = _run(script, env, command)

    assert result.returncode == 0, result.stderr
    assert DEV_GUARD_WARNING in result.stderr


@requires_openssl
def test_no_development_guard_warning_when_prod_is_exported(tmp_path: Path) -> None:
    script, _, env = _ready_project(tmp_path)
    env["HARBORRAG_ENV"] = "prod"

    result = _run(script, env, "worker")

    assert DEV_GUARD_WARNING not in result.stderr
