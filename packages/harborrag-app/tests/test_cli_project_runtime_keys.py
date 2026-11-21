"""``harborrag.yaml`` rejects keys under ``runtime:`` that are not runtime settings."""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_app.cli import main as cli
from harborrag_app.cli.project import ProjectConfigurationError, find_project

pytestmark = pytest.mark.unit


@pytest.fixture
def project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.delenv("HARBORRAG_PROJECT", raising=False)
    return root


def _marker(root: Path, runtime: str) -> None:
    (root / "harborrag.yaml").write_text(f"runtime:\n{runtime}", encoding="utf-8")


def test_find_project_misspelled_runtime_keys_are_named(project_root: Path) -> None:
    _marker(
        project_root,
        "  contol_db_url: postgresql+asyncpg://demo@database.invalid/control\n  tennant_id: DEMO\n",
    )

    with pytest.raises(ProjectConfigurationError, match="contol_db_url, tennant_id"):
        find_project(explicit=str(project_root))


def test_find_project_runtime_keys_match_in_any_case(project_root: Path) -> None:
    _marker(project_root, "  TENANT_ID: DEMO\n  env: dev\n")

    project = find_project(explicit=str(project_root))

    assert project is not None
    assert project.runtime == {"TENANT_ID": "DEMO", "env": "dev"}


def test_main_reports_an_unknown_runtime_key_without_a_traceback(
    project_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _marker(project_root, "  tennant_id: DEMO\n")
    monkeypatch.chdir(project_root)

    assert cli.main(["me", "-o", "json"]) == 1

    err = capsys.readouterr().err
    assert "tennant_id" in err
    assert "Traceback" not in err
