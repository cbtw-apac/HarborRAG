"""How the CLI loads a project marker, and how it falls back to a checkout layout.

Split from test_cli_project.py to keep both modules inside the file-length gate.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from doctor_test_support import unreachable

from harborrag_app.cli.project import activate_project, find_project


def test_a_null_runtime_value_is_treated_as_absent(tmp_path: Path, monkeypatch) -> None:
    """`connector_config_path:` with no value must not export the string "None".

    Exporting it suppresses the real settings default, and the catalog loader then fails
    with a missing-file error that never names the actual cause.
    """

    root = tmp_path / "project"
    root.mkdir()
    (root / "harborrag.yaml").write_text(
        "runtime:\n  connector_config_path:\n  env: dev\n", encoding="utf-8"
    )
    monkeypatch.delenv("HARBORRAG_CONNECTOR_CONFIG_PATH", raising=False)
    monkeypatch.delenv("HARBORRAG_PROJECT", raising=False)

    project = find_project(explicit=str(root))
    assert project is not None
    activate_project(project)

    assert "HARBORRAG_CONNECTOR_CONFIG_PATH" not in os.environ
    assert os.environ["HARBORRAG_ENV"] == "dev"


def test_a_malformed_marker_is_a_project_error(tmp_path: Path, monkeypatch) -> None:
    """yaml.YAMLError is not a ProjectError, so main() would let it become a traceback."""

    from harborrag_app.cli.project import ProjectConfigurationError

    root = tmp_path / "project"
    root.mkdir()
    (root / "harborrag.yaml").write_text("runtime: {oops\n", encoding="utf-8")
    monkeypatch.delenv("HARBORRAG_PROJECT", raising=False)

    with pytest.raises(ProjectConfigurationError, match="could not be read"):
        find_project(explicit=str(root))


def test_main_reports_a_malformed_marker_without_a_traceback(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from harborrag_app.cli import main as cli

    root = tmp_path / "project"
    root.mkdir()
    (root / "harborrag.yaml").write_text("runtime: {oops\n", encoding="utf-8")
    monkeypatch.chdir(root)
    monkeypatch.delenv("HARBORRAG_PROJECT", raising=False)

    assert cli.main(["me", "-o", "json"]) == 1

    captured = capsys.readouterr()
    assert "could not be read" in captured.err
    assert "Traceback" not in captured.err


def test_main_keeps_working_in_a_legacy_checkout_layout(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A checkout has config/sources/connectors.yaml but no harborrag.yaml; that stays supported."""

    from app_test_fixtures import MockAppService

    from harborrag_app.cli import invoke as cli_runner
    from harborrag_app.cli import main as cli

    (tmp_path / "config" / "sources").mkdir(parents=True)
    (tmp_path / "config" / "sources" / "connectors.yaml").write_text("version: 1\nconnectors: {}\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HARBORRAG_PROJECT", raising=False)
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    assert cli.main(["me", "-o", "json"]) == 0
    # The checkout is the CWD here, so the activation notice must stay suppressed.
    assert "using repository checkout" not in capsys.readouterr().err


def test_main_finds_a_legacy_checkout_from_one_of_its_subdirectories(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Catalog paths are CWD-relative, so a checkout must be found *and* moved into.

    Matching config/sources/connectors.yaml against the raw CWD made the CLI work at a checkout's
    root and fail one directory below it -- which is how CI runs the package's own tests.
    """

    from app_test_fixtures import MockAppService

    from harborrag_app.cli import invoke as cli_runner
    from harborrag_app.cli import main as cli

    (tmp_path / "config" / "sources").mkdir(parents=True)
    (tmp_path / "config" / "sources" / "connectors.yaml").write_text("version: 1\nconnectors: {}\n")
    nested = tmp_path / "packages" / "harborrag-app"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    monkeypatch.delenv("HARBORRAG_PROJECT", raising=False)
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)

    assert cli.main(["me", "-o", "json"]) == 0
    assert Path.cwd() == tmp_path.resolve()
    assert "using repository checkout" in capsys.readouterr().err


def _nested_checkout(tmp_path: Path, monkeypatch) -> Path:
    (tmp_path / "config" / "sources").mkdir(parents=True)
    (tmp_path / "config" / "sources" / "connectors.yaml").write_text("version: 1\nconnectors: {}\n")
    nested = tmp_path / "packages" / "harborrag-app"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    monkeypatch.delenv("HARBORRAG_PROJECT", raising=False)
    unreachable(monkeypatch)
    return nested


def test_doctor_walks_up_to_the_checkout_from_a_subdirectory(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The global --project help promises a walk up from the CWD, and doctor keeps it."""

    from harborrag_app.cli import main as cli

    _nested_checkout(tmp_path, monkeypatch)

    cli.main(["doctor", "-o", "json"])

    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)["checks"]}
    assert Path.cwd() == tmp_path.resolve()
    assert checks["project"]["status"] == "ok"
    assert str(tmp_path.resolve()) in checks["project"]["detail"]
    assert checks["connectors catalog"]["status"] == "ok"


def test_doctor_does_not_warn_about_harborrag_yaml_inside_a_checkout(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from harborrag_app.cli import main as cli

    _nested_checkout(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)

    cli.main(["doctor", "-o", "json"])

    project_check = {c["name"]: c for c in json.loads(capsys.readouterr().out)["checks"]}["project"]
    assert project_check["status"] == "ok"
    assert "harborrag.yaml" not in project_check["detail"]
