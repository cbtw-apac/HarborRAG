"""The Docker build context must never carry local state into images.

Every Dockerfile under deploy/docker copies whole package directories (for
example ``COPY packages/harborrag-app ./packages/harborrag-app``), so anything
a developer leaves inside a package -- a SQLite control database holding
tenants, ACLs and encrypted secrets, a ``.local/`` workspace, a ``.harborrag/``
object store or audit log, tool caches -- is otherwise baked into the image.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERIGNORE = ROOT / ".dockerignore"

REQUIRED_EXCLUDES = (
    "**/*.db",
    "**/*.db-journal",
    "**/*.sqlite",
    "**/*.sqlite3",
    "**/.local/",
    "**/.harborrag/",
    "**/.mypy_cache/",
    "**/.pytest_cache/",
    "**/.ruff_cache/",
    "**/.hypothesis/",
    "**/__pycache__/",
    "**/.env",
    "**/.env.*",
    "**/env/",
)


def _patterns() -> list[str]:
    return [
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _excluded(path: str) -> bool:
    """Approximate Docker's matcher: a path is excluded when any run of its
    components matches a ``**/``-anchored pattern, so an excluded directory
    also excludes everything beneath it.

    ``fnmatch``'s ``*`` also crosses ``/``, which is looser than Docker; the
    manual build-context listing in the plan's checklist is the exact check.
    """

    parts = path.split("/")
    runs = [
        "/".join(parts[start : end + 1]) for end in range(len(parts)) for start in range(end + 1)
    ]
    for pattern in _patterns():
        bare = pattern.rstrip("/")
        candidate = bare[3:] if bare.startswith("**/") else bare
        if any(fnmatchcase(run, candidate) for run in runs):
            return True
    return False


@pytest.mark.parametrize("pattern", REQUIRED_EXCLUDES)
def test_dockerignore_excludes_local_state(pattern: str) -> None:
    assert pattern in _patterns()


def test_dockerignore_never_re_includes_anything() -> None:
    assert [pattern for pattern in _patterns() if pattern.startswith("!")] == []


@pytest.mark.parametrize(
    "path",
    [
        "harborrag_control.db",
        "packages/harborrag-app/harborrag_control.db",
        "packages/harborrag-app/harborrag_control.db-journal",
        "packages/harborrag-adapters/.local/harborrag.db",
        "packages/harborrag-runtime/.mypy_cache/3.12/cache.db",
        "packages/harborrag-mcp-server/.harborrag/mcp-audit.jsonl",
        "packages/harborrag-runtime/.harborrag/objects/tenant/blob",
        "packages/harborrag-app/.hypothesis/examples/x",
        "env/.env.database",
        "packages/harborrag-app/.env.local",
    ],
)
def test_known_local_artifacts_are_excluded(path: str) -> None:
    assert _excluded(path), path


@pytest.mark.parametrize(
    "path",
    [
        "uv.lock",
        "config/models.yaml",
        "config/topology/graph_build.yaml",
        "packages/harborrag-app/src/harborrag_app/api/app.py",
        "packages/harborrag-runtime/src/harborrag_runtime/config/settings.py",
    ],
)
def test_build_inputs_stay_in_the_context(path: str) -> None:
    assert not _excluded(path), path
