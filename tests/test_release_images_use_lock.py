from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILES = {
    "api": ROOT / "deploy/docker/Dockerfile.api",
    "cli": ROOT / "deploy/docker/Dockerfile.cli",
    "mcp": ROOT / "deploy/docker/Dockerfile.mcp",
    "worker": ROOT / "deploy/docker/Dockerfile.temporal-worker",
}


@pytest.mark.parametrize("image", DOCKERFILES)
def test_release_image_installs_from_the_frozen_lock(image: str) -> None:
    dockerfile = DOCKERFILES[image].read_text(encoding="utf-8")

    assert "COPY pyproject.toml uv.lock" in dockerfile
    assert "uv export --quiet --frozen --package " in dockerfile
    assert "--no-dev --no-hashes --no-emit-workspace" in dockerfile
    assert "--constraint /tmp/harborrag-lock.txt" in dockerfile
    assert "pip install --upgrade" not in dockerfile
    assert dockerfile.startswith("FROM python:3.12-slim@sha256:")


def test_worker_image_contains_only_worker_packages() -> None:
    dockerfile = DOCKERFILES["worker"].read_text(encoding="utf-8")
    install_block = dockerfile.split("RUN python -m pip install 'uv==", 1)[1]

    assert "-e packages/harborrag-runtime" in install_block
    assert "-e packages/harborrag-app" not in install_block
    assert "-e packages/harborrag-mcp-server" not in install_block
    assert "-e packages/harborrag\n" not in install_block


def test_api_image_contains_only_control_plane_runtime_dependencies() -> None:
    dockerfile = DOCKERFILES["api"].read_text(encoding="utf-8")
    install_block = dockerfile.split("RUN python -m pip install 'uv==", 1)[1]

    assert "--package harborrag-app --extra api" in install_block
    assert "--package harborrag-adapters" in install_block
    assert "--extra control-plane --extra postgres" in install_block
    assert "-e 'packages/harborrag-adapters[control-plane,postgres]'" in install_block
    assert "-e 'packages/harborrag-app[api]'" in install_block
    assert "pdf-docling" not in install_block
    assert "torch==" not in install_block


def test_api_image_bundles_configuration_and_runs_unprivileged() -> None:
    dockerfile = DOCKERFILES["api"].read_text(encoding="utf-8")

    assert "COPY config ./config" in dockerfile
    assert "USER harborrag" in dockerfile
    assert "HEALTHCHECK " in dockerfile
    assert "/api/v1/health" in dockerfile
    assert "build-essential" not in dockerfile
    assert "exec uvicorn harborrag_app.api.app:create_fastapi_app --factory" in dockerfile
    assert '--host \\"$HARBORRAG_HOST\\" --port \\"$HARBORRAG_PORT\\"' in dockerfile


@pytest.mark.parametrize("image", DOCKERFILES)
def test_release_images_run_as_the_unprivileged_application_user(image: str) -> None:
    dockerfile = DOCKERFILES[image].read_text(encoding="utf-8")

    assert "USER harborrag" in dockerfile
    assert "--uid 10001" in dockerfile


def test_mcp_image_does_not_install_unrelated_transports() -> None:
    dockerfile = DOCKERFILES["mcp"].read_text(encoding="utf-8")
    install_block = dockerfile.split("RUN python -m pip install 'uv==", 1)[1]

    assert "-e 'packages/harborrag-mcp-server[mcp]'" in install_block
    assert "-e packages/harborrag-app" not in install_block
    assert "-e packages/harborrag-runtime" not in install_block
    assert "run(transport='stdio')" in dockerfile


def test_worker_uses_the_audited_cpu_torch_release() -> None:
    dockerfile = DOCKERFILES["worker"].read_text(encoding="utf-8")

    assert "--no-deps" in dockerfile
    assert "--index-url https://download.pytorch.org/whl/cpu" in dockerfile
    assert "'torch==2.13.0' 'torchvision==0.28.0'" in dockerfile
    assert "/root/.cache" not in dockerfile
