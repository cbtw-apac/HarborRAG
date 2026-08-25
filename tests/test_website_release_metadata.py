"""Keep public release metadata synchronized across the HarborRAG workspace."""

import json
import re
import tomllib
from pathlib import Path

from release_support.metadata import python_version_to_semver

ROOT = Path(__file__).resolve().parents[1]


def _project(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))["project"]


EXPECTED_VERSION = _project(ROOT / "pyproject.toml")["version"]


def test_workspace_and_public_clients_share_the_release_version() -> None:
    package_projects = sorted((ROOT / "packages").glob("*/pyproject.toml"))
    assert package_projects
    assert {_project(path)["version"] for path in package_projects} == {EXPECTED_VERSION}

    typescript = json.loads((ROOT / "clients/typescript/package.json").read_text(encoding="utf-8"))
    assert typescript["version"] == python_version_to_semver(EXPECTED_VERSION)


def test_internal_package_pins_match_the_release_version() -> None:
    for path in sorted((ROOT / "packages").glob("*/pyproject.toml")):
        project = _project(path)
        requirements = list(project.get("dependencies", []))
        for extra_requirements in project.get("optional-dependencies", {}).values():
            requirements.extend(extra_requirements)

        internal = [item for item in requirements if re.match(r"harborrag(?:-|\[)", item)]
        assert all(f"=={EXPECTED_VERSION}" in item for item in internal), path


def test_api_and_lockfile_report_the_public_release_version() -> None:
    api_source = (ROOT / "packages/harborrag-app/src/harborrag_app/api/app.py").read_text(
        encoding="utf-8"
    )
    assert "version=__version__" in api_source

    lockfile = (ROOT / "uv.lock").read_text(encoding="utf-8")
    for package in (
        "harborrag",
        "harborrag-adapters",
        "harborrag-app",
        "harborrag-core",
        "harborrag-engine",
        "harborrag-mcp-server",
        "harborrag-memory",
        "harborrag-runtime",
        "harborrag-workspace",
    ):
        pattern = rf'name = "{re.escape(package)}"\nversion = "{EXPECTED_VERSION}"'
        assert re.search(pattern, lockfile), package
