from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILES = {
    "api": ROOT / "deploy/docker/Dockerfile.api",
    "cli": ROOT / "deploy/docker/Dockerfile.cli",
    "mcp": ROOT / "deploy/docker/Dockerfile.mcp",
    "worker": ROOT / "deploy/docker/Dockerfile.temporal-worker",
}

# The uv base image ships uv preinstalled, so nothing pip-installs it. It must be a
# glibc build: the locked temporalio, PyTorch, TorchVision and ONNX Runtime releases
# publish manylinux wheels only, so the musl (alpine) variants cannot be used.
BASE_IMAGE = "FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim"
# Everything after this marker is the dependency-installation block.
INSTALL_MARKER = "RUN uv export"


@pytest.mark.parametrize("image", DOCKERFILES)
def test_release_image_installs_from_the_frozen_lock(image: str) -> None:
    dockerfile = DOCKERFILES[image].read_text(encoding="utf-8")

    assert "COPY pyproject.toml uv.lock" in dockerfile
    assert "uv export --quiet --frozen --package " in dockerfile
    assert "--no-dev --no-hashes --no-emit-workspace" in dockerfile
    assert "--constraint /tmp/harborrag-lock.txt" in dockerfile
    assert "pip install --upgrade" not in dockerfile


@pytest.mark.parametrize("image", DOCKERFILES)
def test_release_image_uses_the_shared_glibc_uv_base(image: str) -> None:
    """All four images must share one base so the toolchain cannot drift apart."""
    dockerfile = DOCKERFILES[image].read_text(encoding="utf-8")

    assert dockerfile.splitlines()[0] == BASE_IMAGE


@pytest.mark.parametrize("image", DOCKERFILES)
def test_release_image_installs_into_the_system_environment(image: str) -> None:
    """``uv pip install`` refuses to run outside a virtualenv without ``--system``."""
    dockerfile = DOCKERFILES[image].read_text(encoding="utf-8")

    for line in dockerfile.splitlines():
        if "uv pip install" in line:
            assert "--system" in line, line


def test_worker_image_contains_only_worker_packages() -> None:
    dockerfile = DOCKERFILES["worker"].read_text(encoding="utf-8")
    install_block = dockerfile.split(INSTALL_MARKER, 1)[1]

    assert "-e 'packages/harborrag-runtime[temporal]'" in install_block
    assert "-e packages/harborrag-app" not in install_block
    assert "-e packages/harborrag-mcp-server" not in install_block
    assert "-e packages/harborrag\n" not in install_block


def test_api_image_contains_control_plane_and_retrieval_dependencies() -> None:
    dockerfile = DOCKERFILES["api"].read_text(encoding="utf-8")
    install_block = dockerfile.split(INSTALL_MARKER, 1)[1]

    assert "--extra api --extra production" in install_block
    assert "--package harborrag-adapters" in install_block
    assert "--extra control-plane --extra falkordb --extra llm" in install_block
    assert "--extra postgres --extra qdrant --extra s3" in install_block
    assert (
        "-e 'packages/harborrag-adapters[control-plane,falkordb,llm,postgres,qdrant,s3]'"
        in install_block
    )
    assert "-e 'packages/harborrag-app[api,production]'" in install_block
    assert "pdf-docling" not in install_block
    assert "torch==" not in install_block


def test_api_image_bundles_configuration_and_runs_unprivileged() -> None:
    dockerfile = DOCKERFILES["api"].read_text(encoding="utf-8")

    assert "COPY config ./config" in dockerfile
    assert "USER harborrag" in dockerfile
    assert "HEALTHCHECK " in dockerfile
    assert "/api/v1/health" in dockerfile
    assert "/api/v1/readyz" not in dockerfile
    assert "build-essential" not in dockerfile
    assert "exec uvicorn harborrag_app.api.app:create_fastapi_app --factory" in dockerfile
    assert '--host \\"$HARBORRAG_HOST\\" --port \\"$HARBORRAG_PORT\\"' in dockerfile


@pytest.mark.parametrize("image", DOCKERFILES)
def test_release_images_run_as_the_unprivileged_application_user(image: str) -> None:
    dockerfile = DOCKERFILES[image].read_text(encoding="utf-8")

    assert "USER harborrag" in dockerfile
    assert "--uid 10001" in dockerfile


def test_mcp_image_installs_its_declared_runtime_stack() -> None:
    dockerfile = DOCKERFILES["mcp"].read_text(encoding="utf-8")
    install_block = dockerfile.split(INSTALL_MARKER, 1)[1]

    assert "-e 'packages/harborrag-mcp-server[mcp]'" in install_block
    assert "-e packages/harborrag-app" not in install_block
    assert "-e packages/harborrag-runtime" in install_block
    assert "COPY config ./config" in dockerfile
    assert 'ENTRYPOINT ["python", "-m", "harborrag_mcp_server"]' in dockerfile


def test_worker_uses_the_audited_cpu_torch_release() -> None:
    dockerfile = DOCKERFILES["worker"].read_text(encoding="utf-8")

    assert "--no-deps" in dockerfile
    assert "--index-url https://download.pytorch.org/whl/cpu" in dockerfile
    assert "'torch==2.13.0' 'torchvision==0.28.0'" in dockerfile
    assert "/root/.cache" not in dockerfile
