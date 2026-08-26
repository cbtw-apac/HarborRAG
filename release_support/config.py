"""Static release configuration shared by the release modules."""

from pathlib import Path
from typing import TypedDict


class PackageConfig(TypedDict):
    """Repository location and release policy for one workspace project."""

    path: str
    pyproject: str
    create_release: bool


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_PACKAGE = "harborrag-workspace"
PRIMARY_PACKAGE = "harborrag"

# Keep dependency order here. The public facade is moved to the end again by
# get_packages_for_release() so it is the latest release displayed on GitHub.
PACKAGES: dict[str, PackageConfig] = {
    WORKSPACE_PACKAGE: {
        "path": ".",
        "pyproject": "pyproject.toml",
        "create_release": False,
    },
    "harborrag-core": {
        "path": "packages/harborrag-core",
        "pyproject": "packages/harborrag-core/pyproject.toml",
        "create_release": True,
    },
    "harborrag-adapters": {
        "path": "packages/harborrag-adapters",
        "pyproject": "packages/harborrag-adapters/pyproject.toml",
        "create_release": True,
    },
    "harborrag-memory": {
        "path": "packages/harborrag-memory",
        "pyproject": "packages/harborrag-memory/pyproject.toml",
        "create_release": True,
    },
    "harborrag-engine": {
        "path": "packages/harborrag-engine",
        "pyproject": "packages/harborrag-engine/pyproject.toml",
        "create_release": True,
    },
    "harborrag-runtime": {
        "path": "packages/harborrag-runtime",
        "pyproject": "packages/harborrag-runtime/pyproject.toml",
        "create_release": True,
    },
    "harborrag-app": {
        "path": "packages/harborrag-app",
        "pyproject": "packages/harborrag-app/pyproject.toml",
        "create_release": True,
    },
    "harborrag-mcp-server": {
        "path": "packages/harborrag-mcp-server",
        "pyproject": "packages/harborrag-mcp-server/pyproject.toml",
        "create_release": True,
    },
    PRIMARY_PACKAGE: {
        "path": "packages/harborrag",
        "pyproject": "packages/harborrag/pyproject.toml",
        "create_release": True,
    },
}


def repository_path(relative_path: str) -> Path:
    """Resolve a repository-relative release path without depending on cwd."""

    return REPOSITORY_ROOT / relative_path
