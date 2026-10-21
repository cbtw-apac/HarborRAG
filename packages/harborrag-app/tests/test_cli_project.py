"""The CLI finds a project by its harborrag.yaml marker and activates it."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from doctor_test_support import unreachable
from project_test_support import scaffold as _scaffold

from harborrag_app.cli.project import (
    ProjectNotFoundError,
    activate_project,
    find_project,
    project_option_from_argv,
)


def test_walks_up_from_a_nested_directory(tmp_path: Path) -> None:
    root = _scaffold(tmp_path)
    nested = root / "docs" / "guides"
    nested.mkdir(parents=True)

    project = find_project(nested)

    assert project is not None
    assert project.root == root.resolve()
    assert project.runtime["connector_config_path"] == "config/connectors.yaml"


def test_returns_none_without_a_marker(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("HARBORRAG_PROJECT", raising=False)
    assert find_project(tmp_path) is None


def test_explicit_path_must_contain_a_marker(tmp_path: Path) -> None:
    with pytest.raises(ProjectNotFoundError):
        find_project(tmp_path, explicit=str(tmp_path))


def test_env_variable_selects_the_project(tmp_path: Path, monkeypatch) -> None:
    root = _scaffold(tmp_path / "proj")
    monkeypatch.setenv("HARBORRAG_PROJECT", str(root))

    project = find_project(tmp_path)

    assert project is not None and project.root == root.resolve()


def test_activation_applies_the_documented_precedence(tmp_path: Path, monkeypatch) -> None:
    """process env > .env > harborrag.yaml runtime section > (settings defaults)."""

    root = _scaffold(tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    for name in (
        "OPENAI_API_KEY",
        "HARBORRAG_ENV",
        "HARBORRAG_CONNECTOR_CONFIG_PATH",
        "HARBORRAG_QDRANT_PREFER_GRPC",
        "HARBORRAG_PROJECT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "from-shell")
    project = find_project(root)
    assert project is not None

    activate_project(project)

    assert Path.cwd() == root.resolve()
    assert os.environ["OPENAI_API_KEY"] == "from-shell"  # shell beats .env
    assert os.environ["HARBORRAG_ENV"] == "prod"  # .env beats yaml
    assert os.environ["HARBORRAG_CONNECTOR_CONFIG_PATH"] == str(
        (root / "config/connectors.yaml").resolve()
    )  # yaml paths made absolute
    assert os.environ["HARBORRAG_QDRANT_PREFER_GRPC"] == "false"  # booleans lower-cased


def test_project_option_is_read_from_argv() -> None:
    assert project_option_from_argv(["--project", "/x", "doctor"]) == "/x"
    assert project_option_from_argv(["--project=/y", "doctor"]) == "/y"
    assert project_option_from_argv(["doctor"]) is None


def test_main_activates_the_enclosing_project_before_running(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from app_test_fixtures import MockAppService

    from harborrag_app.cli import main as cli
    from harborrag_app.cli import runner as cli_runner

    root = _scaffold(tmp_path)
    (root / "sub").mkdir()
    monkeypatch.chdir(root / "sub")
    monkeypatch.delenv("HARBORRAG_PROJECT", raising=False)
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    # A marker-only project has no catalogs, so doctor itself reports failures (exit 1);
    # this test only cares that the project was found and activated before it ran.
    unreachable(monkeypatch)
    cli.main(["doctor", "--json"])

    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["checks"][0] == {
        "name": "project",
        "group": "project",
        "status": "ok",
        "detail": str(root.resolve()),
        "hint": "",
        "required": True,
    }
    assert Path.cwd() == root.resolve()
    # conftest exports HARBORRAG_ENV=dev; the shell value must outrank the project .env
    assert os.environ["HARBORRAG_ENV"] == "dev"


def test_main_rejects_a_project_option_without_a_marker(tmp_path: Path, capsys) -> None:
    from harborrag_app.cli import main as cli

    assert cli.main(["--project", str(tmp_path), "doctor", "--json"]) == 1
    assert "does not contain harborrag.yaml" in capsys.readouterr().err


def test_project_value_is_not_mistaken_for_the_command(tmp_path: Path) -> None:
    """`--project DIR retrieve` must resolve the command to `retrieve`, not to DIR.

    If the value-skip regressed, `_requires_project` would be asked about the path and the
    failure would surface as an unrelated missing-project error.
    """

    from harborrag_app.cli import main as cli

    root = _scaffold(tmp_path)

    assert cli._command_name(["--project", str(root), "retrieve", "q"]) == "retrieve"
    assert cli._command_name([f"--project={root}", "retrieve", "q"]) == "retrieve"
    assert cli._command_name(["--no-color", "--project", str(root), "doctor"]) == "doctor"
    assert cli._requires_project(["--project", str(root), "doctor"]) is False
    assert cli._requires_project(["--project", str(root), "retrieve", "q"]) is True


def test_global_value_options_are_all_declared() -> None:
    """`_command_name` hand-parses argv, so its list of value-taking options must be whole.

    Adding a value-taking option to `configure()` without listing it there would make that
    option's value read as the sub-command name. Derive the truth from Click and compare.
    """

    import typer.main

    from harborrag_app.cli import main as cli

    declared = {
        option
        for parameter in typer.main.get_command(cli.app).params
        if not getattr(parameter, "is_flag", False)
        for option in parameter.opts
        if option.startswith("--")
    }

    assert declared == set(cli._VALUE_TAKING_GLOBAL_OPTIONS)


def test_main_explains_how_to_start_when_no_project_exists(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Outside a project (and outside a checkout) the CLI must point at `harborrag init`."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HARBORRAG_PROJECT", raising=False)
    from harborrag_app.cli import main as cli

    assert cli.main(["retrieve", "anything", "--json"]) == 1
    err = capsys.readouterr().err
    assert "harborrag init" in err and "--project" in err


def test_walk_up_rejects_a_marker_in_a_directory_owned_by_someone_else(
    tmp_path: Path, monkeypatch
) -> None:
    """Like git's safe.directory: an ancestor another user controls must not configure us."""

    from harborrag_app.cli import project as project_module
    from harborrag_app.cli.project import UnsafeProjectError

    root = _scaffold(tmp_path)
    nested = root / "scratch"
    nested.mkdir()
    monkeypatch.delenv("HARBORRAG_PROJECT", raising=False)
    someone_else = os.geteuid() + 1
    monkeypatch.setattr(project_module.os, "geteuid", lambda: someone_else)

    with pytest.raises(UnsafeProjectError, match="not owned by you"):
        find_project(nested)


def test_walk_up_rejects_a_world_writable_marker_directory(tmp_path: Path, monkeypatch) -> None:
    from harborrag_app.cli.project import UnsafeProjectError

    root = _scaffold(tmp_path)
    nested = root / "docs"
    nested.mkdir()
    root.chmod(0o777)
    monkeypatch.delenv("HARBORRAG_PROJECT", raising=False)

    with pytest.raises(UnsafeProjectError, match="world-writable"):
        find_project(nested)


def test_explicit_project_selection_is_an_opt_in_and_skips_the_safety_check(
    tmp_path: Path, monkeypatch
) -> None:
    from harborrag_app.cli import project as project_module

    root = _scaffold(tmp_path)
    someone_else = os.geteuid() + 1
    monkeypatch.setattr(project_module.os, "geteuid", lambda: someone_else)

    project = find_project(explicit=str(root))

    assert project is not None and project.root == root.resolve()


def test_main_announces_activation_when_it_leaves_the_current_directory(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from app_test_fixtures import MockAppService

    from harborrag_app.cli import main as cli
    from harborrag_app.cli import runner as cli_runner

    root = _scaffold(tmp_path)
    (root / "sub").mkdir()
    monkeypatch.chdir(root / "sub")
    monkeypatch.delenv("HARBORRAG_PROJECT", raising=False)
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    unreachable(monkeypatch)
    cli.main(["doctor", "--json"])

    assert f"using project {root.resolve()}" in capsys.readouterr().err


def test_help_does_not_activate_a_project(tmp_path: Path, monkeypatch, capsys) -> None:
    from harborrag_app.cli import main as cli

    root = _scaffold(tmp_path)
    (root / "sub").mkdir()
    monkeypatch.chdir(root / "sub")
    monkeypatch.delenv("HARBORRAG_PROJECT", raising=False)

    assert cli.main(["--help"]) == 0

    assert Path.cwd() == (root / "sub").resolve()
    assert "using project" not in capsys.readouterr().err


def test_unknown_command_is_reported_as_such(tmp_path: Path, monkeypatch, capsys) -> None:
    from harborrag_app.cli import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HARBORRAG_PROJECT", raising=False)

    assert cli.main(["bogus"]) == 2

    err = capsys.readouterr().err
    assert "No such command" in err
    assert "no project" not in err


def test_root_help_describes_auth_keys(capsys) -> None:
    from harborrag_app.cli import main as cli

    assert cli.main(["--help"]) == 0

    assert "auth keys" in " ".join(capsys.readouterr().out.split())


def test_init_help_shows_default_once(capsys) -> None:
    from harborrag_app.cli import main as cli

    assert cli.main(["init", "--help"]) == 0

    import re

    text = " ".join(capsys.readouterr().out.split())
    # `default: ./docs` (another option) is not the DIR default.
    assert len(re.findall(r"default: \.(?![\w/])", text)) == 1


@pytest.fixture
def _pristine_harborrag_logger():
    import logging

    logger = logging.getLogger("harborrag")
    saved = (list(logger.handlers), list(logger.filters), logger.level, logger.propagate)
    logger.handlers = []
    logger.filters = []
    logger.setLevel(logging.NOTSET)
    try:
        yield logger
    finally:
        logger.handlers, logger.filters = saved[0], saved[1]
        logger.setLevel(saved[2])
        logger.propagate = saved[3]


@pytest.mark.parametrize(("level", "expect_traceback"), [("WARNING", False), ("DEBUG", True)])
def test_tracebacks_are_debug_only(
    _pristine_harborrag_logger, level: str, expect_traceback: bool, monkeypatch
) -> None:
    import io
    import logging

    from harborrag_app.cli import main as cli
    from harborrag_core.observability import process_logging

    stream = io.StringIO()
    monkeypatch.setattr(process_logging.sys, "stderr", stream)
    logger = cli._configure_cli_logging(level)
    assert cli._configure_cli_logging(level) is logger  # idempotent, no stacked filters
    assert sum(len(handler.filters) for handler in logger.handlers) == 1

    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logging.getLogger("harborrag.app.cli").warning("call failed", exc_info=True)

    output = stream.getvalue()
    assert "call failed" in output
    assert ("Traceback" in output) is expect_traceback
    assert ("set HARBORRAG_LOG_LEVEL=DEBUG for the traceback" in output) is not expect_traceback
