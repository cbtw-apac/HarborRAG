"""Keep public release metadata synchronized across the HarborRAG workspace."""

import json
import re
import tomllib
from pathlib import Path

from release_support.config import PACKAGES, PRIMARY_PACKAGE, release_tag
from release_support.metadata import python_version_to_semver
from release_support.versions import get_packages_for_release
from release_support.workflows import CRITICAL_WORKFLOWS

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

    lock = json.loads((ROOT / "clients/typescript/package-lock.json").read_text(encoding="utf-8"))
    assert lock["version"] == typescript["version"]
    assert lock["packages"][""]["version"] == typescript["version"]


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
    for package in PACKAGES:
        pattern = rf'name = "{re.escape(package)}"\nversion = "{EXPECTED_VERSION}"'
        assert re.search(pattern, lockfile), package


def _workflow(name: str) -> str:
    return (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")


def test_publish_workflow_accepts_exactly_the_released_packages() -> None:
    """publish.yml keeps its own package list; it must match release_support."""

    case_arm = re.search(
        r"(?m)^\s*((?:harborrag[\w-]*\|)+harborrag[\w-]*)\)\s*$", _workflow("publish.yml")
    )
    assert case_arm is not None
    assert set(case_arm.group(1).split("|")) == set(get_packages_for_release())
    # The workflow recovers the package by stripping the "-v<version>" suffix.
    assert '"${TAG_NAME%-v*}"' in _workflow("publish.yml")
    assert release_tag("harborrag-core", "9.9.9") == "harborrag-core-v9.9.9"


def test_docs_deploy_matches_the_primary_release_tag() -> None:
    assert f"== {release_tag(PRIMARY_PACKAGE, '')}* ]]" in _workflow("docs-auto.yml")


def test_website_footer_links_the_primary_release_tag() -> None:
    base = (ROOT / "website" / "templates" / "base.html").read_text(encoding="utf-8")
    js_tag = release_tag(PRIMARY_PACKAGE, "${encodeURIComponent(data.version)}")

    assert f"/releases/tag/{js_tag}`" in base


def test_release_gate_workflows_exist() -> None:
    """A renamed workflow would otherwise block every release as 'missing'."""

    names = {
        match.group(1).strip().strip("\"'")
        for path in (ROOT / ".github" / "workflows").glob("*.yml")
        for match in [re.search(r"(?m)^name:\s*(.+)$", path.read_text(encoding="utf-8"))]
        if match
    }
    assert CRITICAL_WORKFLOWS <= names
