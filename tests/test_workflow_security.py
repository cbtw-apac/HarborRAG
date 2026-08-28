"""Supply-chain invariants for privileged GitHub Actions workflows."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.workflow]

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
ACTION_USE = re.compile(r"^\s*-?\s*uses:\s*[^@\s]+@([^\s#]+)", re.MULTILINE)
COMMIT_SHA = re.compile(r"[0-9a-f]{40}")


def _workflow(name: str) -> dict[str, object]:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def test_every_third_party_action_is_pinned_to_a_commit() -> None:
    unpinned: list[str] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        content = path.read_text(encoding="utf-8")
        for reference in ACTION_USE.findall(content):
            if COMMIT_SHA.fullmatch(reference) is None:
                unpinned.append(f"{path.name}: @{reference}")

    assert not unpinned, f"mutable GitHub Action references: {unpinned}"


def test_checkout_credentials_are_not_persisted() -> None:
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = _workflow(path.name)
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                if str(step.get("uses", "")).startswith("actions/checkout@"):
                    assert step.get("with", {}).get("persist-credentials") is False, path.name


@pytest.mark.parametrize("name", ["docs-auto.yml", "docs-manual.yml"])
def test_docs_build_jobs_do_not_receive_deployment_credentials(name: str) -> None:
    workflow = _workflow(name)
    assert workflow["permissions"] == {"contents": "read"}
    build_permissions = workflow["jobs"]["build-docs"]["permissions"]
    assert build_permissions == {"actions": "read", "contents": "read"}
    assert "pages" not in build_permissions
    assert "id-token" not in build_permissions
    assert workflow["jobs"]["deploy"]["permissions"] == {
        "pages": "write",
        "id-token": "write",
    }


def test_automatic_docs_build_does_not_mask_failed_release_checks() -> None:
    condition = _workflow("docs-auto.yml")["jobs"]["build-docs"]["if"]

    assert "!cancelled()" in condition
    assert "always()" not in condition


def test_contract_client_build_is_locked_and_audited() -> None:
    workflow = (WORKFLOWS / "contract.yml").read_text(encoding="utf-8")

    assert (ROOT / "clients/typescript/package-lock.json").is_file()
    assert "npm ci --ignore-scripts" in workflow
    assert "npm audit --audit-level=moderate" in workflow
    assert "npm ci || npm install" not in workflow
    assert "tufin/oasdiff:v1.23.0" in workflow
    assert "tufin/oasdiff:v1.23.0@sha256:" not in workflow


def test_python_lock_is_audited_in_quality_gates() -> None:
    workflow = (WORKFLOWS / "quality-gates.yml").read_text(encoding="utf-8")

    assert "uv export --frozen --all-packages --all-extras --no-dev" in workflow
    assert "uvx --from pip-audit==2.10.1 pip-audit" in workflow


def test_documentation_builds_pass_an_explicit_public_origin() -> None:
    """Canonical, Open Graph and sitemap URLs must never rely on the fallback.

    ``--base-url ""`` is correct for a root-served deployment, so the absolute
    origin has to arrive separately. Without ``--site-url`` the builder emits
    the hardcoded default and a host change silently ships wrong URLs.
    """
    for name in ("docs-auto.yml", "docs-manual.yml"):
        content = (WORKFLOWS / name).read_text(encoding="utf-8")
        assert "website/build.py" in content, name
        assert "--site-url" in content, f"{name} builds the site without an explicit origin"
