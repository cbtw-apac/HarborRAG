"""Regression checks for the supported local development entrypoints."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
API_COMPOSE = ROOT / "deploy/compose/docker-compose.yml"
DEV_SCRIPT = ROOT / "scripts/deployment/dev.sh"
MCP_SCRIPT = ROOT / "scripts/deployment/mcp.sh"


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


@pytest.mark.parametrize(
    ("arguments", "expected_output", "expected_docker_call"),
    (
        (
            ("--build", "up"),
            "Building Temporal ingestion worker image",
            "--profile worker up --build",
        ),
        (
            ("up",),
            "Reusing local worker image",
            "--profile worker up --no-build",
        ),
        (
            ("down",),
            "Stopping HarborRAG API",
            "docker-compose.temporal.yml --profile worker down",
        ),
    ),
)
def test_development_entrypoint_supports_documented_argument_forms(
    tmp_path: Path,
    arguments: tuple[str, ...],
    expected_output: str,
    expected_docker_call: str,
) -> None:
    project = tmp_path / "project"
    script = project / "scripts/deployment/dev.sh"
    script.parent.mkdir(parents=True)
    script.write_text(DEV_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")

    environment = project / "env"
    environment.mkdir()
    for filename in (
        ".env.database",
        ".env.temporal",
        ".env.connector",
        ".env.parser",
        ".env.models",
        ".env.api",
    ):
        (environment / filename).write_text("", encoding="utf-8")
    (environment / ".env.database").write_text(
        "HARBORRAG_SECRETS_ENCRYPTION_KEY=test-encryption-key\n",
        encoding="utf-8",
    )
    (environment / ".env.mcp").write_text(
        "HARBORRAG_MCP_BEARER_TOKEN=test-token\n",
        encoding="utf-8",
    )
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

    result = subprocess.run(
        ["bash", str(script), *arguments],
        cwd=project,
        env={
            **os.environ,
            "DEV_DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert expected_output in result.stdout
    docker_calls = docker_log.read_text(encoding="utf-8")
    assert expected_docker_call in docker_calls
    if arguments == ("--build", "up"):
        assert "Building HarborRAG API image" in result.stdout
        assert "docker-compose.yml up --build" in docker_calls


def test_api_and_worker_reuse_local_images_unless_rebuild_is_requested() -> None:
    api = API_COMPOSE.read_text(encoding="utf-8")
    temporal = (ROOT / "deploy/compose/docker-compose.temporal.yml").read_text(encoding="utf-8")
    script = DEV_SCRIPT.read_text(encoding="utf-8")

    assert "image: ${HARBORRAG_API_IMAGE:-harborrag-api-api}" in api
    assert (
        "image: ${HARBORRAG_TEMPORAL_WORKER_IMAGE:-harborrag-temporal-temporal-worker}" in temporal
    )
    assert 'docker image inspect "${API_IMAGE}"' in script
    # start_worker derives a device-specific tag from TEMPORAL_WORKER_IMAGE
    # (the -gpu suffix under --device gpu) and gates the rebuild on that tag.
    assert 'local worker_image="${TEMPORAL_WORKER_IMAGE}"' in script
    assert 'docker image inspect "${worker_image}"' in script
    assert "local -a build_args=(--no-build)" in script
    assert "build_args=(--build)" in script
    assert "api [--build]" in script
    assert "worker [--build]" in script


def test_monitoring_credentials_are_configured_outside_the_development_script() -> None:
    script = DEV_SCRIPT.read_text(encoding="utf-8")
    database_example = (ROOT / "env-example/.env.database.example").read_text(encoding="utf-8")
    monitoring_example = (ROOT / "env-example/.env.monitoring.example").read_text(encoding="utf-8")

    assert "MONITORING_ENV_FILE" not in script
    assert "ensure_monitoring_environment_file" not in script
    assert "env-example/.env.monitoring.example" not in script
    assert "GRAFANA_ADMIN_PASSWORD" not in database_example
    assert "HARBORRAG_MONITORING_BIND_ADDRESS=127.0.0.1" in monitoring_example
    assert "GRAFANA_ADMIN_PASSWORD=\n" in monitoring_example


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


def test_mcp_entrypoint_runs_the_server_container_without_other_services() -> None:
    mcp_script = MCP_SCRIPT.read_text(encoding="utf-8")
    dev_script = DEV_SCRIPT.read_text(encoding="utf-8")

    assert "deploy/compose/docker-compose.mcp.yml" in mcp_script
    assert 'run --rm --no-deps -T mcp --transport stdio "$@"' in mcp_script
    assert "HARBORRAG_CONTROL_DB_URL" not in mcp_script
    assert "HARBORRAG_MODEL_CONFIG_PATH" not in mcp_script
    assert "source " not in mcp_script
    assert "start_worker" not in mcp_script
    assert "start_api" not in mcp_script
    assert "docker-compose.yml" not in mcp_script
    assert "docker-compose.temporal.yml" not in mcp_script
    assert "start_mcp" not in dev_script
    assert "    mcp)" not in dev_script


def _mcp_project(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    project = tmp_path / "project"
    script = project / "scripts/deployment/mcp.sh"
    script.parent.mkdir(parents=True)
    script.write_text(MCP_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    environment = project / "env"
    environment.mkdir()
    (environment / ".env.database").write_text(
        "POSTGRES_USER=test\nHARBORRAG_MCP_DB_PASSWORD=test-db-password\n"
        "HARBORRAG_MCP_OBJECT_STORE_SECRET_ACCESS_KEY=test-object-secret\n",
        encoding="utf-8",
    )
    (environment / ".env.mcp").write_text(
        "HARBORRAG_MCP_BEARER_TOKEN=test-token\nHARBORRAG_MCP_PORT=8123\n",
        encoding="utf-8",
    )

    fake_bin = project / "fake-bin"
    fake_bin.mkdir()
    docker_log = project / "docker.log"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/usr/bin/env sh
printf '%s\\n' "$* HARBORRAG_ENV=${HARBORRAG_ENV:-}" >> "$MCP_DOCKER_LOG"
case " $* " in
  *" image inspect "*) [ -n "$MCP_IMAGE_PRESENT" ] ;;
esac
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    env = {
        **os.environ,
        "MCP_DOCKER_LOG": str(docker_log),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }
    return script, docker_log, env


@pytest.mark.parametrize(
    ("arguments", "image_present", "expected_calls", "unexpected_calls"),
    (
        (
            ("--check",),
            "1",
            ("run --rm --no-deps -T mcp --transport stdio --check",),
            (" build mcp",),
        ),
        (
            ("--check",),
            "",
            (" build mcp", "run --rm --no-deps -T mcp --transport stdio --check"),
            (),
        ),
        (
            ("--build", "--http"),
            "1",
            (" build mcp", "up --no-build --detach --wait --wait-timeout 120 mcp"),
            (" run ",),
        ),
        (("down",), "1", ("docker-compose.mcp.yml down",), (" build ", " run ")),
    ),
)
def test_mcp_entrypoint_drives_the_mcp_compose_service(
    tmp_path: Path,
    arguments: tuple[str, ...],
    image_present: str,
    expected_calls: tuple[str, ...],
    unexpected_calls: tuple[str, ...],
) -> None:
    script, docker_log, env = _mcp_project(tmp_path)

    result = subprocess.run(
        ["bash", str(script), *arguments],
        env={**env, "MCP_IMAGE_PRESENT": image_present},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    docker_calls = docker_log.read_text(encoding="utf-8")
    assert "--env-file" in docker_calls
    for call in expected_calls:
        assert call in docker_calls
    for call in unexpected_calls:
        assert call not in docker_calls
    if arguments[-1] == "--http":
        assert "http://127.0.0.1:8123/" in result.stdout
    else:
        # Stdio and check mode must keep stdout free for the MCP protocol.
        assert result.stdout == ""


def test_mcp_entrypoint_requires_bootstrapped_environment(tmp_path: Path) -> None:
    script, docker_log, env = _mcp_project(tmp_path)
    (script.parents[2] / "env/.env.mcp").unlink()

    result = subprocess.run(
        ["bash", str(script), "--check"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 2
    assert "env/.env.mcp" in result.stderr
    assert "dev.sh bootstrap" in result.stderr
    assert not docker_log.exists()


def test_deployment_has_explicit_orchestration_and_mcp_entrypoints() -> None:
    scripts = sorted((ROOT / "scripts/deployment").glob("*.sh"))

    assert [script.name for script in scripts] == ["dev.sh", "mcp-ui.sh", "mcp.sh"]
    assert all(script.stat().st_mode & stat.S_IXUSR for script in scripts)


def test_api_has_one_canonical_compose_file() -> None:
    assert API_COMPOSE.is_file()
    for obsolete_name in (
        "docker-compose.dev.yml",
        "docker-compose.prod.yml",
        "docker-compose.all.yml",
    ):
        assert not (API_COMPOSE.parent / obsolete_name).exists()


def test_api_restarts_and_mounts_graph_build_policy() -> None:
    compose = API_COMPOSE.read_text(encoding="utf-8")

    assert "restart: unless-stopped" in compose
    assert "HARBORRAG_GRAPH_BUILD_CONFIG_PATH: /app/config/topology/graph_build.yaml" in compose
    assert "../../config:/app/config:ro" in compose


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


def test_mcp_entrypoint_refuses_to_start_without_the_reader_role_password(tmp_path: Path) -> None:
    script, docker_log, env = _mcp_project(tmp_path)
    (script.parents[2] / "env/.env.database").write_text(
        "POSTGRES_USER=test\nHARBORRAG_MCP_DB_PASSWORD=\n", encoding="utf-8"
    )

    result = subprocess.run(
        ["bash", str(script), "--check"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 2
    assert "HARBORRAG_MCP_DB_PASSWORD" in result.stderr
    assert "dev.sh mcp-role" in result.stderr
    assert not docker_log.exists()


def _dev_project(tmp_path: Path, database_env: str) -> tuple[Path, Path, Path, dict[str, str]]:
    """A checkout with a fake docker that logs argv and captures psql's stdin."""

    project = tmp_path / "project"
    script = project / "scripts/deployment/dev.sh"
    script.parent.mkdir(parents=True)
    script.write_text(DEV_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    sql = project / "deploy/postgres/mcp-reader-role.sql"
    sql.parent.mkdir(parents=True)
    sql.write_text("-- role sql\n", encoding="utf-8")
    policy = project / "deploy/minio/mcp-reader-policy.json"
    policy.parent.mkdir(parents=True)
    policy.write_text('{"Statement": []}\n', encoding="utf-8")
    environment = project / "env"
    environment.mkdir()
    for filename in (".env.temporal", ".env.connector", ".env.parser", ".env.models", ".env.api"):
        (environment / filename).write_text("", encoding="utf-8")
    (environment / ".env.database").write_text(database_env, encoding="utf-8")
    (environment / ".env.mcp").write_text(
        "HARBORRAG_MCP_BEARER_TOKEN=test-token\n", encoding="utf-8"
    )
    (project / "env-example").mkdir()
    (project / "env-example/.env.database.example").write_text("", encoding="utf-8")

    fake_bin = project / "fake-bin"
    fake_bin.mkdir()
    docker_log = project / "docker.log"
    docker_stdin = project / "docker.stdin"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/usr/bin/env sh
printf '%s\\n' "$*" >> "$DEV_DOCKER_LOG"
case " $* " in
  *" exec -T postgres sh -c "*) cat > "$DEV_DOCKER_STDIN" ;;
  *" exec -T minio sh -s"*) cat > "$DEV_DOCKER_STDIN.minio" ;;
  *" config --services "*) printf 'api\\n' ;;
esac
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    env = {
        **os.environ,
        "DEV_DOCKER_LOG": str(docker_log),
        "DEV_DOCKER_STDIN": str(docker_stdin),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }
    return script, docker_log, docker_stdin, env


def test_mcp_role_command_provisions_the_reader_role_without_exposing_the_password(
    tmp_path: Path,
) -> None:
    script, docker_log, docker_stdin, env = _dev_project(
        tmp_path,
        "POSTGRES_USER=owner\nPOSTGRES_DB=harbor\n"
        "HARBORRAG_MCP_DB_USER=harborrag_mcp_reader\nHARBORRAG_MCP_DB_PASSWORD=s3cret-pw\n"
        "MINIO_ROOT_USER=root\nMINIO_ROOT_PASSWORD=root-pw\n"
        "HARBORRAG_MCP_OBJECT_STORE_ACCESS_KEY_ID=harborrag-mcp-reader\n"
        "HARBORRAG_MCP_OBJECT_STORE_SECRET_ACCESS_KEY=obj-s3cret\n",
    )

    result = subprocess.run(
        ["bash", str(script), "mcp-role"],
        cwd=script.parents[2],
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "MCP database role 'harborrag_mcp_reader' is ready." in result.stdout
    docker_calls = docker_log.read_text(encoding="utf-8")
    assert "docker-compose.database.yml exec -T postgres sh -c" in docker_calls
    # Owner login and database name are the container's own variables; only the
    # generated role name crosses over, as a positional argument.
    assert '--username "$POSTGRES_USER" --dbname "$POSTGRES_DB"' in docker_calls
    assert "sh harborrag_mcp_reader" in docker_calls
    # The password reaches psql only on stdin, never in the process arguments.
    assert "s3cret-pw" not in docker_calls
    stdin = docker_stdin.read_text(encoding="utf-8")
    assert stdin.startswith("\\set mcp_password 's3cret-pw'\n")
    assert "-- role sql" in stdin
    # MinIO: the mc script, root and reader credentials included, is stdin-only.
    assert "MCP object-store user 'harborrag-mcp-reader' is ready." in result.stdout
    assert "docker-compose.database.yml exec -T minio sh -s" in docker_calls
    for secret in ("root-pw", "obj-s3cret"):
        assert secret not in docker_calls
    minio_stdin = Path(f"{docker_stdin}.minio").read_text(encoding="utf-8")
    # Root credentials are the container's own variables; they never leave it.
    assert (
        'mc alias set --quiet local http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"'
        in minio_stdin
    )
    assert "root-pw" not in minio_stdin
    assert "mc admin user add local 'harborrag-mcp-reader' 'obj-s3cret'" in minio_stdin
    assert '{"Statement": []}' in minio_stdin
    assert (
        "mc admin policy attach local harborrag-mcp-reader --user 'harborrag-mcp-reader'"
        in minio_stdin
    )


def test_mcp_role_command_requires_a_generated_password(tmp_path: Path) -> None:
    script, docker_log, _, env = _dev_project(
        tmp_path, "POSTGRES_USER=owner\nHARBORRAG_MCP_DB_USER=harborrag_mcp_reader\n"
    )

    result = subprocess.run(
        ["bash", str(script), "mcp-role"],
        cwd=script.parents[2],
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 2
    assert "HARBORRAG_MCP_DB_PASSWORD" in result.stderr
    assert "dev.sh bootstrap" in result.stderr
    assert not docker_log.exists()


def test_bootstrap_adds_reader_role_credentials_to_an_existing_database_env(
    tmp_path: Path,
) -> None:
    script, docker_log, _, env = _dev_project(tmp_path, "POSTGRES_USER=owner\n")

    result = subprocess.run(
        ["bash", str(script), "bootstrap"],
        cwd=script.parents[2],
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    database_env = (script.parents[2] / "env/.env.database").read_text(encoding="utf-8")
    assert "POSTGRES_USER=owner\n" in database_env
    assert "HARBORRAG_MCP_DB_USER=harborrag_mcp_reader\n" in database_env
    password = next(
        line.split("=", 1)[1]
        for line in database_env.splitlines()
        if line.startswith("HARBORRAG_MCP_DB_PASSWORD=")
    )
    assert len(password) == 64 and set(password) <= set("0123456789abcdef")
    assert "HARBORRAG_MCP_OBJECT_STORE_ACCESS_KEY_ID=harborrag-mcp-reader\n" in database_env
    object_secret = next(
        line.split("=", 1)[1]
        for line in database_env.splitlines()
        if line.startswith("HARBORRAG_MCP_OBJECT_STORE_SECRET_ACCESS_KEY=")
    )
    assert len(object_secret) == 40  # MinIO caps secret keys at 40 characters
    assert "dev.sh mcp-role" in result.stdout or "mcp-role" in result.stdout
    assert not docker_log.exists()
    # Re-running keeps the generated password.
    again = subprocess.run(
        ["bash", str(script), "bootstrap"],
        cwd=script.parents[2],
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )
    assert again.returncode == 0, again.stderr
    assert password in (script.parents[2] / "env/.env.database").read_text(encoding="utf-8")


def test_mcp_role_quotes_operator_credentials_for_the_container_shell(tmp_path: Path) -> None:
    # A secret with a quote and a space must reach mc intact, not abort
    # provisioning or break the generated shell script.
    script, docker_log, docker_stdin, env = _dev_project(
        tmp_path,
        "POSTGRES_USER=owner\nPOSTGRES_DB=harbor\n"
        "HARBORRAG_MCP_DB_USER=harborrag_mcp_reader\nHARBORRAG_MCP_DB_PASSWORD=s3cret-pw\n"
        "MINIO_ROOT_USER=root\nMINIO_ROOT_PASSWORD=root-pw\n"
        "HARBORRAG_MCP_OBJECT_STORE_ACCESS_KEY_ID=harborrag-mcp-reader\n"
        "HARBORRAG_MCP_OBJECT_STORE_SECRET_ACCESS_KEY=it's a pass\n",
    )

    result = subprocess.run(
        ["bash", str(script), "mcp-role"],
        cwd=script.parents[2],
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    minio_stdin = Path(f"{docker_stdin}.minio").read_text(encoding="utf-8")
    assert "mc admin user add local 'harborrag-mcp-reader' 'it'\\''s a pass'" in minio_stdin
    # The generated script itself parses: run it through sh -n.
    check = subprocess.run(
        ["sh", "-n"], input=minio_stdin, capture_output=True, text=True, check=False
    )
    assert check.returncode == 0, check.stderr
    assert "it's a pass" not in docker_log.read_text(encoding="utf-8")


def test_mcp_role_migrate_starts_the_api_before_provisioning(tmp_path: Path) -> None:
    # --migrate defers to the API, which owns the schema: it must be up and
    # healthy (migrations applied) before any GRANT names a table.
    script, docker_log, _, env = _dev_project(
        tmp_path,
        "POSTGRES_USER=owner\nPOSTGRES_DB=harbor\nHARBORRAG_SECRETS_ENCRYPTION_KEY=k\n"
        "HARBORRAG_MCP_DB_USER=harborrag_mcp_reader\nHARBORRAG_MCP_DB_PASSWORD=s3cret-pw\n"
        "MINIO_ROOT_USER=root\nMINIO_ROOT_PASSWORD=root-pw\n"
        "HARBORRAG_MCP_OBJECT_STORE_ACCESS_KEY_ID=harborrag-mcp-reader\n"
        "HARBORRAG_MCP_OBJECT_STORE_SECRET_ACCESS_KEY=obj-s3cret\n",
    )
    (script.parents[2] / "docs").mkdir(exist_ok=True)

    result = subprocess.run(
        ["bash", str(script), "mcp-role", "--migrate"],
        cwd=script.parents[2],
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    calls = docker_log.read_text(encoding="utf-8")
    api_up = calls.index("docker-compose.yml up")
    role = calls.index("exec -T postgres sh -c")
    assert api_up < role < calls.index("exec -T minio sh -s")

    plain = subprocess.run(
        ["bash", str(script), "--build", "mcp-role"],
        cwd=script.parents[2],
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )
    assert plain.returncode == 2 and "--migrate" in plain.stderr


def test_mcp_entrypoint_gives_the_server_the_apis_environment(tmp_path: Path) -> None:
    # Keys are issued with the API's HARBORRAG_ENV; the server must run with the same one.
    script, docker_log, env = _mcp_project(tmp_path)
    (script.parents[2] / "env/.env.api").write_text("HARBORRAG_ENV=prod\n", encoding="utf-8")
    env = {k: v for k, v in env.items() if k != "HARBORRAG_ENV"}

    result = subprocess.run(
        ["bash", str(script), "--check"],
        env={**env, "MCP_IMAGE_PRESENT": "1"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "HARBORRAG_ENV=prod" in docker_log.read_text(encoding="utf-8")
