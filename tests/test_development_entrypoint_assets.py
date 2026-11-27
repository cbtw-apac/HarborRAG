"""Regression checks for the supported local development entrypoints."""

from __future__ import annotations

import os
import re
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
API_COMPOSE = ROOT / "deploy/compose/docker-compose.yml"
DEV_SCRIPT = ROOT / "scripts/deployment/dev.sh"
MCP_SCRIPT = ROOT / "scripts/deployment/mcp.sh"
MCP_LAUNCHER = ROOT / "scripts/deployment/common/mcp-launcher.sh"
DEV_CHECKS = ROOT / "scripts/deployment/common/dev-checks.sh"
OBSERVABILITY_SCRIPT = ROOT / "scripts/deployment/observability.sh"


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
    assert "docker-compose.monitoring.yml" not in script
    assert "observability.sh" not in script
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
        (
            ("sharepoint-login",),
            "Signing in to SharePoint for the worker",
            "--profile worker run --rm --no-deps temporal-worker"
            " python -m harborrag_adapters.connectors.sharepoint.login",
        ),
        (
            ("sharepoint-login", "--logout"),
            "Signing in to SharePoint for the worker",
            "harborrag_adapters.connectors.sharepoint.login --logout",
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
    _copy_dev_checks(script)

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
    entrypoint = MCP_SCRIPT.read_text(encoding="utf-8")
    launcher = MCP_LAUNCHER.read_text(encoding="utf-8")
    mcp_script = entrypoint + launcher
    dev_script = DEV_SCRIPT.read_text(encoding="utf-8")

    assert 'MCP_COMPOSE_FILE="docker-compose.mcp.yml"' in entrypoint
    assert 'MCP_SERVICE="mcp"' in entrypoint
    assert 'run --rm --no-deps -T "${MCP_SERVICE}" --transport stdio "$@"' in launcher
    assert "HARBORRAG_CONTROL_DB_URL" not in mcp_script
    assert "HARBORRAG_MODEL_CONFIG_PATH" not in mcp_script
    # The shared launcher is the only file sourced; env files go to Compose.
    sources = [
        line.strip() for line in mcp_script.splitlines() if line.strip().startswith("source ")
    ]
    assert sources == ['source "${ROOT_DIR}/scripts/deployment/common/mcp-launcher.sh"']
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
    launcher = project / "scripts/deployment/common/mcp-launcher.sh"
    launcher.parent.mkdir()
    launcher.write_text(MCP_LAUNCHER.read_text(encoding="utf-8"), encoding="utf-8")
    environment = project / "env"
    environment.mkdir()
    (environment / ".env.database").write_text("POSTGRES_USER=test\n", encoding="utf-8")
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
printf '%s\\n' "$* HARBORRAG_ENV=${HARBORRAG_ENV:-} HARBORRAG_MCP_PORT=${HARBORRAG_MCP_PORT:-} \\
HARBORRAG_GIT_REVISION=${HARBORRAG_GIT_REVISION:-}" >> "$MCP_DOCKER_LOG"
case " $* " in
  *" image inspect --format "*) [ -n "$MCP_IMAGE_PRESENT" ] && printf '%s\\n' "${MCP_IMAGE_REVISION:-}" ;;
  *" image inspect "*) [ -n "$MCP_IMAGE_PRESENT" ] ;;
  *" network inspect "*) [ -z "${MCP_NETWORK_ABSENT:-}" ] ;;
  *" --preflight "*) [ -z "${MCP_PREFLIGHT_FAILS:-}" ] ;;
  *" up "*) [ -z "${MCP_UP_FAILS:-}" ] ;;
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


def _run_mcp(
    script: Path, env: dict[str, str], *arguments: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *arguments],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
    )


def _notes(result: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in result.stderr.splitlines() if line.startswith("Note:")]


_COMMITTED_SOURCE = "VALUE = 1\n"
_EDITED_SOURCE = "VALUE = 2\n"


def _commit_source(project: Path) -> str:
    """Make the project a git checkout with one committed source file; return the commit."""

    (project / "packages").mkdir()
    (project / "packages/module.py").write_text(_COMMITTED_SOURCE, encoding="utf-8")
    git = ["git", "-C", str(project), "-c", "user.name=Test", "-c", "user.email=test@example.com"]
    subprocess.run(["git", "init", "--quiet", str(project)], check=True)
    subprocess.run([*git, "add", "packages"], check=True)
    subprocess.run([*git, "commit", "--quiet", "--message", "source"], check=True)
    head = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
    return head.stdout.strip()


@pytest.mark.unit
@pytest.mark.parametrize("arguments", ((), ("--build", "--http")))
def test_mcp_entrypoint_names_the_fix_when_the_data_network_is_missing(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    script, docker_log, env = _mcp_project(tmp_path)

    result = _run_mcp(
        script, {**env, "MCP_IMAGE_PRESENT": "1", "MCP_NETWORK_ABSENT": "1"}, *arguments
    )

    assert result.returncode == 2
    assert "harborrag-data-network" in result.stderr
    assert "scripts/deployment/dev.sh data" in result.stderr
    docker_calls = docker_log.read_text(encoding="utf-8")
    assert " build " not in docker_calls
    assert " run " not in docker_calls


@pytest.mark.unit
def test_mcp_check_runs_the_image_without_any_network(tmp_path: Path) -> None:
    script, docker_log, env = _mcp_project(tmp_path)

    result = _run_mcp(
        script, {**env, "MCP_IMAGE_PRESENT": "1", "MCP_NETWORK_ABSENT": "1"}, "--check"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    docker_calls = docker_log.read_text(encoding="utf-8")
    assert "run --rm --network none" in docker_calls
    assert "/config:/app/config:ro" in docker_calls
    assert "harborrag-mcp-mcp --transport stdio --check" in docker_calls
    assert " network inspect " not in docker_calls
    assert "run --rm --no-deps" not in docker_calls


@pytest.mark.unit
def test_mcp_entrypoint_stops_the_server_without_the_data_network(tmp_path: Path) -> None:
    script, docker_log, env = _mcp_project(tmp_path)

    result = _run_mcp(script, {**env, "MCP_NETWORK_ABSENT": "1"}, "down")

    assert result.returncode == 0, result.stderr
    assert "docker-compose.mcp.yml down" in docker_log.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("arguments", "image_present", "expected_calls", "unexpected_calls"),
    (
        (
            ("--check",),
            "1",
            ("run --rm --network none", "harborrag-mcp-mcp --transport stdio --check"),
            (" build mcp", "run --rm --no-deps"),
        ),
        (
            ("--check",),
            "",
            (" build mcp", "harborrag-mcp-mcp --transport stdio --check"),
            ("run --rm --no-deps",),
        ),
        (
            (),
            "1",
            ("run --rm --no-deps -T mcp --transport stdio",),
            (" build mcp", "--network none"),
        ),
        (
            ("--build", "--http"),
            "1",
            (
                " build mcp",
                "run --rm --no-deps -T mcp --http --preflight",
                "up --no-build --detach --wait --wait-timeout 120 mcp",
            ),
            ("--transport stdio",),
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
    if arguments[-1:] == ("--http",):
        assert "http://127.0.0.1:8123/" in result.stdout
    else:
        # Stdio and check mode must keep stdout free for the MCP protocol.
        assert result.stdout == ""


@pytest.mark.unit
def test_mcp_http_start_checks_the_configuration_before_starting(tmp_path: Path) -> None:
    script, docker_log, env = _mcp_project(tmp_path)

    result = _run_mcp(script, {**env, "MCP_IMAGE_PRESENT": "1"}, "--http")

    assert result.returncode == 0, result.stderr
    calls = docker_log.read_text(encoding="utf-8")
    assert calls.index("--http --preflight") < calls.index("up --no-build --detach --wait")


@pytest.mark.unit
def test_mcp_entrypoint_publishes_the_port_its_env_file_names(tmp_path: Path) -> None:
    script, docker_log, env = _mcp_project(tmp_path)

    result = _run_mcp(script, {**env, "MCP_IMAGE_PRESENT": "1"}, "--http")

    assert result.returncode == 0, result.stderr
    (start,) = [
        line for line in docker_log.read_text(encoding="utf-8").splitlines() if " up " in line
    ]
    assert "HARBORRAG_MCP_PORT=8123" in start


@pytest.mark.unit
@pytest.mark.parametrize(
    "port_line",
    [
        pytest.param('HARBORRAG_MCP_PORT="8124"', id="double-quoted"),
        pytest.param("HARBORRAG_MCP_PORT='8124'", id="single-quoted"),
        pytest.param("HARBORRAG_MCP_PORT=8124   # the playground", id="trailing-comment"),
        pytest.param("HARBORRAG_MCP_PORT=8124\r", id="windows-line-ending"),
    ],
)
def test_mcp_entrypoint_reads_a_port_the_way_compose_reads_env_files(
    tmp_path: Path, port_line: str
) -> None:
    script, docker_log, env = _mcp_project(tmp_path)
    (script.parents[2] / "env/.env.mcp").write_text(
        f"HARBORRAG_MCP_BEARER_TOKEN=test-token\n{port_line}\n", encoding="utf-8", newline=""
    )

    result = _run_mcp(script, {**env, "MCP_IMAGE_PRESENT": "1"}, "--http")

    assert result.returncode == 0, result.stderr
    (start,) = [
        line for line in docker_log.read_text(encoding="utf-8").splitlines() if " up " in line
    ]
    assert "HARBORRAG_MCP_PORT=8124 " in start
    assert "http://127.0.0.1:8124/" in result.stdout


@pytest.mark.unit
def test_mcp_entrypoint_refuses_a_port_that_is_not_a_number(tmp_path: Path) -> None:
    script, docker_log, env = _mcp_project(tmp_path)
    (script.parents[2] / "env/.env.mcp").write_text(
        "HARBORRAG_MCP_BEARER_TOKEN=test-token\nHARBORRAG_MCP_PORT=eighty\n", encoding="utf-8"
    )

    result = _run_mcp(script, {**env, "MCP_IMAGE_PRESENT": "1"}, "--http")

    assert result.returncode == 2
    assert "HARBORRAG_MCP_PORT" in result.stderr
    assert not docker_log.exists() or " up " not in docker_log.read_text(encoding="utf-8")


@pytest.mark.unit
def test_mcp_http_start_stops_when_the_preflight_fails(tmp_path: Path) -> None:
    script, docker_log, env = _mcp_project(tmp_path)

    result = _run_mcp(
        script, {**env, "MCP_IMAGE_PRESENT": "1", "MCP_PREFLIGHT_FAILS": "1"}, "--http"
    )

    assert result.returncode == 2
    assert "was not started" in result.stderr
    assert "env/.env.mcp" in result.stderr
    assert "env/.env.models" in result.stderr
    assert " up " not in docker_log.read_text(encoding="utf-8")


@pytest.mark.unit
def test_mcp_http_start_removes_a_server_that_never_becomes_healthy(tmp_path: Path) -> None:
    script, docker_log, env = _mcp_project(tmp_path)

    result = _run_mcp(script, {**env, "MCP_IMAGE_PRESENT": "1", "MCP_UP_FAILS": "1"}, "--http")

    assert result.returncode == 2
    assert "did not become healthy" in result.stderr
    calls = docker_log.read_text(encoding="utf-8")
    assert (
        calls.index(" up ")
        < calls.index("logs --tail 20 mcp")
        < calls.index("docker-compose.mcp.yml down")
    )


def _project_with_templates(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """An MCP project whose template and model configuration name the settings they need."""

    script, _, env = _mcp_project(tmp_path)
    project = script.parents[2]
    (project / "env-example").mkdir()
    (project / "env-example/.env.mcp.example").write_text(
        "HARBORRAG_MCP_BEARER_TOKEN=\n"
        "HARBORRAG_MCP_PORT=8010\n"
        "HARBORRAG_QDRANT_PREFER_GRPC=false\n"
        "# HARBORRAG_MCP_AUTH_MODE=api_key\n",
        encoding="utf-8",
    )
    (project / "config/models").mkdir(parents=True)
    (project / "config/models/models.yaml").write_text(
        "gateway:\n"
        "  url: ${HARBORRAG_URL}\n"
        "  key: ${HARBORRAG_API_KEY}\n"
        "  model: ${HARBORRAG_EMBED_MODEL:-default-embedding}\n"
        "  # retired: ${HARBORRAG_RETIRED}\n",
        encoding="utf-8",
    )
    return script, {**env, "MCP_IMAGE_PRESENT": "1"}


@pytest.mark.unit
def test_mcp_launcher_names_the_model_variables_no_env_file_sets(tmp_path: Path) -> None:
    script, env = _project_with_templates(tmp_path)
    (script.parents[2] / "env/.env.models").write_text(
        "HARBORRAG_API_KEY=placeholder\n", encoding="utf-8"
    )

    result = _run_mcp(script, env, "--http")

    assert result.returncode == 0, result.stderr
    (model_note,) = _notes(result)
    assert "env/.env.models" in model_note
    assert "HARBORRAG_URL" in model_note
    for satisfied in ("HARBORRAG_API_KEY", "HARBORRAG_EMBED_MODEL", "HARBORRAG_RETIRED"):
        assert satisfied not in model_note


@pytest.mark.unit
def test_mcp_launcher_is_silent_about_a_setting_left_to_its_default(tmp_path: Path) -> None:
    script, env = _project_with_templates(tmp_path)
    (script.parents[2] / "env/.env.models").write_text(
        "HARBORRAG_URL=https://gateway.example\nHARBORRAG_API_KEY=placeholder\n",
        encoding="utf-8",
    )

    result = _run_mcp(script, env, "--http")

    assert result.returncode == 0, result.stderr
    assert _notes(result) == []
    assert "HARBORRAG_QDRANT_PREFER_GRPC" not in result.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    ("recorded", "source_now", "expected_note"),
    (
        ("{head}", _COMMITTED_SOURCE, None),
        ("{head}", _EDITED_SOURCE, "was built from"),
        ("", _COMMITTED_SOURCE, "does not record a commit of this checkout"),
        pytest.param(
            "ea456083" + "0" * 32,
            _COMMITTED_SOURCE,
            "does not record a commit of this checkout",
            id="revision-inherited-from-the-base-image",
        ),
        ("{head}-dirty", _EDITED_SOURCE, None),
    ),
)
def test_mcp_launcher_warns_when_the_image_predates_the_source(
    tmp_path: Path, recorded: str, source_now: str, expected_note: str | None
) -> None:
    script, _, env = _mcp_project(tmp_path)
    project = script.parents[2]
    head = _commit_source(project)
    (project / "packages/module.py").write_text(source_now, encoding="utf-8")

    result = _run_mcp(
        script,
        {**env, "MCP_IMAGE_PRESENT": "1", "MCP_IMAGE_REVISION": recorded.format(head=head)},
        "--check",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    if expected_note is None:
        assert _notes(result) == []
    else:
        (note,) = _notes(result)
        assert expected_note in note
        assert "--build" in note


@pytest.mark.unit
def test_mcp_image_build_records_the_source_commit(tmp_path: Path) -> None:
    script, docker_log, env = _mcp_project(tmp_path)
    project = script.parents[2]
    head = _commit_source(project)

    committed = _run_mcp(script, {**env, "MCP_IMAGE_PRESENT": ""}, "--check")
    (project / "packages/module.py").write_text("VALUE = 2\n", encoding="utf-8")
    edited = _run_mcp(script, {**env, "MCP_IMAGE_PRESENT": ""}, "--check")

    assert (committed.returncode, edited.returncode) == (0, 0), committed.stderr + edited.stderr
    builds = [
        line
        for line in docker_log.read_text(encoding="utf-8").splitlines()
        if " build mcp " in line
    ]
    assert builds[0].endswith(f"HARBORRAG_GIT_REVISION={head}")
    assert builds[1].endswith(f"HARBORRAG_GIT_REVISION={head}-dirty")


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

    assert [script.name for script in scripts] == [
        "dev.sh",
        "mcp-ui.sh",
        "mcp.sh",
        "observability.sh",
    ]
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
    assert "HARBORRAG_GRAPH_BUILD_CONFIG_PATH: /app/config/graph/build.yaml" in compose
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


def test_mcp_entrypoint_gives_the_server_the_apis_environment(tmp_path: Path) -> None:
    # Keys are issued with the API's HARBORRAG_ENV; the server must run with the same one.
    script, docker_log, env = _mcp_project(tmp_path)
    (script.parents[2] / "env/.env.api").write_text("HARBORRAG_ENV=prod\n", encoding="utf-8")
    env = {k: v for k, v in env.items() if k != "HARBORRAG_ENV"}

    result = subprocess.run(
        ["bash", str(script)],
        env={**env, "MCP_IMAGE_PRESENT": "1"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "HARBORRAG_ENV=prod" in docker_log.read_text(encoding="utf-8")


def test_bootstrap_generates_a_missing_encryption_key_once(tmp_path: Path) -> None:
    run = _bootstrap_project(tmp_path, database_volume=False)

    def bootstrap() -> str:
        result = run()
        assert result.returncode == 0, result.stderr
        database = (tmp_path / "env/.env.database").read_text(encoding="utf-8")
        keys = re.findall(r"^HARBORRAG_SECRETS_ENCRYPTION_KEY=(.*)$", database, re.MULTILINE)
        assert len(keys) == 1 and re.fullmatch(r"[0-9a-f]{64}", keys[0])
        return keys[0]

    first = bootstrap()

    assert bootstrap() == first
    assert (tmp_path / "env/.env.database").stat().st_mode & 0o777 == 0o600


def test_bootstrap_never_generates_a_key_for_an_existing_database(tmp_path: Path) -> None:
    result = _bootstrap_project(tmp_path, database_volume=True)()

    database = (tmp_path / "env/.env.database").read_text(encoding="utf-8")
    assert result.returncode == 2
    assert "compose_pg_data already holds a database" in result.stderr
    assert re.search(r"^HARBORRAG_SECRETS_ENCRYPTION_KEY=$", database, re.MULTILINE)


def _bootstrap_project(
    tmp_path: Path, *, database_volume: bool
) -> Callable[[], subprocess.CompletedProcess[str]]:
    """Copy dev.sh and the env templates; return a runner of ``bootstrap`` with a fake docker.

    The fake docker answers ``volume inspect`` as if the database volume exists or not.
    """
    script = tmp_path / "scripts/deployment/dev.sh"
    script.parent.mkdir(parents=True)
    script.write_text(DEV_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    _copy_dev_checks(script)
    for template in (ROOT / "env-example").glob(".env.*.example"):
        target = tmp_path / "env-example" / template.name
        target.parent.mkdir(exist_ok=True)
        target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(f"#!/usr/bin/env sh\nexit {0 if database_volume else 1}\n", encoding="utf-8")
    docker.chmod(0o755)
    environ = {k: v for k, v in os.environ.items() if k != "HARBORRAG_SECRETS_ENCRYPTION_KEY"}
    environ["PATH"] = f"{fake_bin}:{environ['PATH']}"

    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(script), "bootstrap"],
            cwd=tmp_path,
            env=environ,
            capture_output=True,
            check=False,
            text=True,
        )

    return run


def _copy_dev_checks(script: Path) -> None:
    """Place dev.sh's sourced checks next to a copied dev.sh."""
    checks = script.parent / "common/dev-checks.sh"
    checks.parent.mkdir(exist_ok=True)
    checks.write_text(DEV_CHECKS.read_text(encoding="utf-8"), encoding="utf-8")


@pytest.mark.parametrize("folder_exists", [True, False])
def test_dev_api_mounts_the_folder_the_workers_read(tmp_path: Path, folder_exists: bool) -> None:
    project = tmp_path / "project"
    script = project / "scripts/deployment/dev.sh"
    script.parent.mkdir(parents=True)
    script.write_text(DEV_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    _copy_dev_checks(script)
    environment = project / "env"
    environment.mkdir()
    for filename in (".env.temporal", ".env.parser", ".env.models", ".env.api"):
        (environment / filename).write_text("", encoding="utf-8")
    (environment / ".env.database").write_text(
        "HARBORRAG_SECRETS_ENCRYPTION_KEY=test-encryption-key\n", encoding="utf-8"
    )
    (environment / ".env.connector").write_text("LOCAL_SOURCE_PATH=./notes\n", encoding="utf-8")
    if folder_exists:
        (project / "notes").mkdir()
    fake_bin = project / "fake-bin"
    fake_bin.mkdir()
    docker_log = project / "docker.log"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/usr/bin/env sh
printf '%s source_dir=%s\\n' "$*" "${HARBORRAG_LOCAL_SOURCE_DIR:-}" >> "$DEV_DOCKER_LOG"
case " $* " in
  *" config --services "*) printf 'api\\n' ;;
esac
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    result = subprocess.run(
        ["bash", str(script), "api"],
        cwd=project,
        env={
            **{k: v for k, v in os.environ.items() if k != "HARBORRAG_LOCAL_SOURCE_DIR"},
            "DEV_DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        capture_output=True,
        check=False,
        text=True,
    )

    calls = docker_log.read_text(encoding="utf-8") if docker_log.exists() else ""
    api_up = [line for line in calls.splitlines() if "docker-compose.yml up" in line]
    if folder_exists:
        assert result.returncode == 0, result.stderr
        assert api_up and api_up[0].endswith(f"source_dir={project / 'notes'}")
    else:
        assert result.returncode == 2
        assert "Local connector source directory does not exist" in result.stderr
        assert api_up == []


@pytest.mark.parametrize(
    ("compose_file", "service"),
    [
        ("deploy/compose/docker-compose.yml", "api"),
        ("deploy/compose/docker-compose.temporal.yml", "temporal-worker"),
    ],
)
def test_api_and_worker_share_the_container_local_source_root(
    compose_file: str, service: str
) -> None:
    document = yaml.safe_load((ROOT / compose_file).read_text(encoding="utf-8"))

    environment = document["services"][service]["environment"]

    assert environment["HARBORRAG_LOCAL_SOURCE_ROOT"] == "/data/sources"


@pytest.mark.parametrize(
    "compose_file",
    ["deploy/compose/docker-compose.yml", "deploy/compose/docker-compose.temporal.yml"],
)
def test_api_and_worker_share_one_tenant_setting(compose_file: str) -> None:
    text = (ROOT / compose_file).read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    service = "api" if compose_file.endswith("docker-compose.yml") else "temporal-worker"

    environment = document["services"][service]["environment"]

    assert environment["HARBORRAG_TENANT_ID"] == "${HARBORRAG_TENANT_ID:-DEFAULT}"


def test_the_worker_receives_the_removed_tenant_name_so_it_refuses_to_start() -> None:
    worker = yaml.safe_load(
        (ROOT / "deploy/compose/docker-compose.temporal.yml").read_text(encoding="utf-8")
    )["services"]["temporal-worker"]["environment"]
    api = (ROOT / "deploy/compose/docker-compose.yml").read_text(encoding="utf-8")

    assert worker["HARBORRAG_INGESTION_TENANT_ID"] == "${HARBORRAG_INGESTION_TENANT_ID:-}"
    assert "INGESTION_TENANT_ID" not in api


_OBSERVABILITY_FAKE_DOCKER = """#!/usr/bin/env sh
printf '%s POSTGRES_DB=%s\\n' "$*" "${POSTGRES_DB:-}" >> "$DOCKER_LOG"
case " $* " in
  *" network inspect "*) [ -z "${NETWORK_ABSENT:-}" ] ;;
  *" exec "*" postgres "*)
    cat > "$DOCKER_STDIN"
    printf '%s' "${HARBORRAG_OBSERVER_PASSWORD:-}" > "$DOCKER_STDIN.password" ;;
esac
"""


def _observability_project(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """Copy the launcher, its helpers, template and role SQL; return it with a fake docker."""
    for relative in (
        "scripts/deployment/observability.sh",
        "scripts/deployment/common/dev-checks.sh",
        "env-example/.env.monitoring.example",
        "deploy/compose/postgres/observer-role.sql",
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / relative).read_bytes())
    (tmp_path / "env").mkdir()
    (tmp_path / "env/.env.database").write_text(
        "POSTGRES_USER=owner\nPOSTGRES_PASSWORD=owner-secret\nPOSTGRES_DB=harborrag\n",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "docker").write_text(_OBSERVABILITY_FAKE_DOCKER, encoding="utf-8")
    (fake_bin / "docker").chmod(0o755)
    env = {
        **{key: value for key, value in os.environ.items() if key != "POSTGRES_DB"},
        "DOCKER_LOG": str(tmp_path / "docker.log"),
        "DOCKER_STDIN": str(tmp_path / "stdin"),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }
    return tmp_path / "scripts/deployment/observability.sh", env


def _run_observability(
    script: Path, env: dict[str, str], *arguments: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *arguments],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
    )


def _docker_calls(tmp_path: Path) -> str:
    log = tmp_path / "docker.log"
    return log.read_text(encoding="utf-8") if log.exists() else ""


@pytest.mark.unit
def test_observability_entrypoint_documents_its_commands() -> None:
    script = OBSERVABILITY_SCRIPT.read_text(encoding="utf-8")

    assert OBSERVABILITY_SCRIPT.stat().st_mode & stat.S_IXUSR
    assert script.startswith("#!/usr/bin/env bash\nset -euo pipefail\n")
    for command in (
        "  bootstrap ",
        "  up ",
        "  down [--volumes] ",
        "  status ",
        "  logs [SERVICE...] ",
        "  db-access ",
    ):
        assert command in script
    assert 'MONITORING_ENV_FILE="${MONITORING_ENV_FILE:-env/.env.monitoring}"' in script


@pytest.mark.unit
def test_observability_bootstrap_writes_protected_generated_passwords_once(
    tmp_path: Path,
) -> None:
    script, env = _observability_project(tmp_path)
    monitoring = tmp_path / "env/.env.monitoring"

    def passwords() -> list[str]:
        result = _run_observability(script, env, "bootstrap")
        assert result.returncode == 0, result.stderr
        text = monitoring.read_text(encoding="utf-8")
        values = [
            re.findall(rf"^{name}=(.*)$", text, re.MULTILINE)
            for name in ("GRAFANA_ADMIN_PASSWORD", "HARBORRAG_OBSERVER_PASSWORD")
        ]
        assert all(len(found) == 1 and re.fullmatch(r"[0-9a-f]{64}", found[0]) for found in values)
        return [found[0] for found in values]

    first = passwords()

    assert passwords() == first
    assert monitoring.stat().st_mode & 0o777 == 0o600
    assert _docker_calls(tmp_path) == ""


@pytest.mark.unit
def test_observability_up_passes_compose_only_the_database_name(tmp_path: Path) -> None:
    script, env = _observability_project(tmp_path)
    assert _run_observability(script, env, "bootstrap").returncode == 0

    result = _run_observability(script, env, "up")

    assert result.returncode == 0, result.stderr
    compose_calls = [line for line in _docker_calls(tmp_path).splitlines() if "compose" in line]
    assert any("docker-compose.monitoring.yml up --detach --wait" in line for line in compose_calls)
    assert all(line.endswith("POSTGRES_DB=harborrag") for line in compose_calls)
    assert not any(".env.database" in line for line in compose_calls)
    assert "OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318" in result.stdout
    for restart in ("dev.sh api", "dev.sh worker", "mcp.sh --http"):
        assert restart in result.stdout


@pytest.mark.unit
def test_observability_up_names_the_fix_when_the_data_network_is_missing(
    tmp_path: Path,
) -> None:
    script, env = _observability_project(tmp_path)
    assert _run_observability(script, env, "bootstrap").returncode == 0

    result = _run_observability(script, {**env, "NETWORK_ABSENT": "1"}, "up")

    assert result.returncode == 2
    assert "scripts/deployment/dev.sh data" in result.stderr
    assert " up " not in _docker_calls(tmp_path)


@pytest.mark.unit
def test_observability_db_access_hands_the_password_over_outside_argv(tmp_path: Path) -> None:
    script, env = _observability_project(tmp_path)
    assert _run_observability(script, env, "bootstrap").returncode == 0
    password = re.findall(
        r"^HARBORRAG_OBSERVER_PASSWORD=(.*)$",
        (tmp_path / "env/.env.monitoring").read_text(encoding="utf-8"),
        re.MULTILINE,
    )[0]

    result = _run_observability(script, env, "db-access")

    assert result.returncode == 0, result.stderr
    calls = _docker_calls(tmp_path)
    assert "docker-compose.database.yml exec -T -e HARBORRAG_OBSERVER_PASSWORD postgres" in calls
    assert password not in calls
    assert (tmp_path / "stdin.password").read_text(encoding="utf-8") == password
    assert (tmp_path / "stdin").read_bytes() == (
        ROOT / "deploy/compose/postgres/observer-role.sql"
    ).read_bytes()
