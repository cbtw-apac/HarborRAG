"""The Explorer MCP UI server is an optional add-on to the reader MCP server."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI_SCRIPT = ROOT / "scripts/deployment/mcp-ui.sh"
READER_SCRIPT = ROOT / "scripts/deployment/mcp.sh"
LAUNCHER = ROOT / "scripts/deployment/lib/mcp-launcher.sh"
UI_COMPOSE = ROOT / "deploy/compose/docker-compose.mcp-ui.yml"
READER_COMPOSE = ROOT / "deploy/compose/docker-compose.mcp.yml"
UI_DOCKERFILE = ROOT / "deploy/docker/Dockerfile.mcp-ui"
READER_DOCKERFILE = ROOT / "deploy/docker/Dockerfile.mcp"


def _ui_project(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    project = tmp_path / "project"
    script = project / "scripts/deployment/mcp-ui.sh"
    script.parent.mkdir(parents=True)
    script.write_text(UI_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    launcher = project / "scripts/deployment/lib/mcp-launcher.sh"
    launcher.parent.mkdir()
    launcher.write_text(LAUNCHER.read_text(encoding="utf-8"), encoding="utf-8")
    environment = project / "env"
    environment.mkdir()
    (environment / ".env.database").write_text("POSTGRES_USER=test\n", encoding="utf-8")
    (environment / ".env.mcp").write_text(
        "HARBORRAG_MCP_BEARER_TOKEN=test-token\nHARBORRAG_MCP_UI_PORT=8124\n",
        encoding="utf-8",
    )
    fake_bin = project / "fake-bin"
    fake_bin.mkdir()
    docker_log = project / "docker.log"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/usr/bin/env sh
printf '%s\\n' "$*" >> "$MCP_DOCKER_LOG"
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


def _run(script: Path, env: dict[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *arguments],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
    )


@pytest.mark.parametrize(
    ("arguments", "image_present", "expected_calls", "unexpected_calls"),
    (
        (
            ("--check",),
            "1",
            ("run --rm --no-deps -T mcp-ui --transport stdio --check",),
            (" build ",),
        ),
        (
            ("--check",),
            "",
            (
                "docker-compose.mcp.yml build mcp",
                "docker-compose.mcp-ui.yml build mcp-ui",
                "run --rm --no-deps -T mcp-ui --transport stdio --check",
            ),
            (),
        ),
        (
            ("--build", "--http"),
            "1",
            (
                "docker-compose.mcp.yml build mcp",
                "docker-compose.mcp-ui.yml build mcp-ui",
                "up --no-build --detach --wait --wait-timeout 120 mcp-ui",
            ),
            (" run ",),
        ),
        (("down",), "1", ("docker-compose.mcp-ui.yml down",), (" build ", " run ")),
    ),
)
def test_ui_entrypoint_drives_its_own_compose_service(
    tmp_path: Path,
    arguments: tuple[str, ...],
    image_present: str,
    expected_calls: tuple[str, ...],
    unexpected_calls: tuple[str, ...],
) -> None:
    script, docker_log, env = _ui_project(tmp_path)

    result = _run(script, {**env, "MCP_IMAGE_PRESENT": image_present}, *arguments)

    assert result.returncode == 0, result.stderr
    calls = docker_log.read_text(encoding="utf-8")
    for call in expected_calls:
        assert call in calls
    for call in unexpected_calls:
        assert call not in calls
    # It never starts, stops or rebuilds anything through the reader's project
    # except the shared base image.
    assert "docker-compose.mcp.yml down" not in calls
    assert "docker-compose.mcp.yml up" not in calls
    if len(expected_calls) == 3 and "build" in expected_calls[0]:
        # The Explorer image layers over the reader image, so that builds first.
        assert calls.index("build mcp\n") < calls.index("build mcp-ui")
    if arguments[-1] == "--http":
        assert "http://127.0.0.1:8124/mcp" in result.stdout
    else:
        # Stdio and check mode must keep stdout free for the MCP protocol.
        assert result.stdout == ""


def test_ui_entrypoint_requires_bootstrapped_environment(tmp_path: Path) -> None:
    script, docker_log, env = _ui_project(tmp_path)
    (script.parents[2] / "env/.env.mcp").unlink()

    result = _run(script, env, "--check")

    assert result.returncode == 2
    assert "env/.env.mcp" in result.stderr
    assert "dev.sh bootstrap" in result.stderr
    assert not docker_log.exists()


def test_the_reader_deployment_does_not_depend_on_the_ui() -> None:
    # The shared launcher is part of the reader deployment, so it must stay UI-free too.
    reader_script = READER_SCRIPT.read_text(encoding="utf-8") + LAUNCHER.read_text(encoding="utf-8")
    reader_compose = READER_COMPOSE.read_text(encoding="utf-8")
    reader_dockerfile = READER_DOCKERFILE.read_text(encoding="utf-8")

    for text in (reader_script, reader_compose, reader_dockerfile):
        assert "mcp-ui" not in text
        assert "[ui]" not in text and ",ui]" not in text


def test_the_ui_image_layers_the_optional_extra_over_the_reader_image() -> None:
    dockerfile = UI_DOCKERFILE.read_text(encoding="utf-8")
    compose = UI_COMPOSE.read_text(encoding="utf-8")

    assert "FROM ${HARBORRAG_MCP_IMAGE}" in dockerfile
    assert "--extra ui" in dockerfile
    assert "--frozen" in dockerfile
    assert "packages/harborrag-mcp-server[reader,ui]" in dockerfile
    assert 'ENTRYPOINT ["harborrag-mcp-ui"]' in dockerfile
    assert "USER harborrag" in dockerfile
    assert "name: harborrag-mcp-ui" in compose
    assert "service: mcp" in compose
    assert "harborrag_mcp_ui_audit:/var/lib/harborrag/.harborrag" in compose
    assert "HARBORRAG_MCP_UI_PORT','8011'" in compose


_WITHOUT_UI_EXTRA = """
import contextlib, io, json, sys
sys.modules["prefab_ui"] = None  # as if harborrag-mcp-server[ui] were not installed
from harborrag_mcp_server.__main__ import main, ui_main
out = io.StringIO()
with contextlib.redirect_stdout(out):
    code = main(["--check"])
print(json.dumps({"code": code, "tools": json.loads(out.getvalue())}))
try:
    ui_main(["--check"])
except SystemExit as exc:
    print(json.dumps({"ui_exit": exc.code}))
"""


def test_the_reader_server_works_without_the_optional_ui_extra(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _WITHOUT_UI_EXTRA],
        cwd=tmp_path,
        env={**os.environ, "HARBORRAG_MCP_AUDIT_PATH": str(tmp_path / "audit.jsonl")},
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
    )

    lines = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    assert lines[0]["code"] == 0, result.stderr
    assert len(lines[0]["tools"]) == 11
    assert "explorer_search" not in lines[0]["tools"]
    assert lines[1] == {"ui_exit": 2}
    assert "harborrag-mcp-server[ui]" in result.stderr
