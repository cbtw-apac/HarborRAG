"""``harborrag --version`` prints one line and exits before any project or service loads."""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest
from project_test_support import scaffold

import harborrag_app
from harborrag_app.cli import main as cli

pytestmark = pytest.mark.unit

_VERSION_LINE = re.compile(r"harborrag \S+\n")


@pytest.fixture(autouse=True)
def _nothing_may_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("--version must not load a project or environment")

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HARBORRAG_PROJECT", raising=False)
    monkeypatch.setattr(cli, "load_project_environment", refuse)
    monkeypatch.setattr(cli, "activate_from_argv", refuse)


def test_version_prints_one_line_and_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["--version"])

    captured = capsys.readouterr()
    assert code == 0
    assert _VERSION_LINE.fullmatch(captured.out)
    assert captured.err == ""


def test_version_ignores_a_missing_project_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["--project", str(tmp_path / "missing"), "--version"])

    assert code == 0
    assert _VERSION_LINE.fullmatch(capsys.readouterr().out)


def test_version_ignores_a_broken_project_marker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = scaffold(tmp_path / "project")
    (root / "harborrag.yaml").write_text("runtime: {oops\n", encoding="utf-8")

    code = cli.main(["--project", str(root), "--version"])

    assert code == 0
    assert _VERSION_LINE.fullmatch(capsys.readouterr().out)


@pytest.mark.parametrize("command", [["projects", "list"], ["doctor"], ["settings", "get"]])
def test_version_with_a_command_prints_the_version_only(
    command: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["--version", *command])

    assert code == 0
    assert _VERSION_LINE.fullmatch(capsys.readouterr().out)


def test_version_is_the_app_version_when_the_umbrella_is_not_installed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def not_installed(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(cli, "distribution_version", not_installed)

    code = cli.main(["--version"])

    assert code == 0
    assert capsys.readouterr().out == f"harborrag {harborrag_app.__version__}\n"


def test_version_is_the_umbrella_version_when_installed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "distribution_version", lambda name: "9.8.7")

    assert cli.main(["--version"]) == 0

    assert capsys.readouterr().out == "harborrag 9.8.7\n"


def test_version_is_listed_in_the_help(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "load_project_environment", lambda: ())
    assert cli.main(["--help"]) == 0

    assert "--version" in capsys.readouterr().out
