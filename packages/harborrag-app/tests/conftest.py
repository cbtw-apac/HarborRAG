"""Security-safe process defaults shared by API package tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer import rich_utils

from harborrag_app.cli import environment as environment_module
from harborrag_app.cli import project as project_module

SHELL_VARIABLES = (
    "HARBORRAG_LOCAL_SOURCE_ROOT",
    "HARBORRAG_TENANT_ID",
    "HARBORRAG_INGESTION_TENANT_ID",
)


@pytest.fixture(autouse=True)
def _isolated_application_environment(
    _restore_process_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Use loopback auth and a durable database isolated to each test.

    Drops the variables a developer's shell may export (``SHELL_VARIABLES``), and pins
    ``COLUMNS`` so Rich does not wrap usage boxes at a narrow terminal's width; a test
    that needs one of them sets it.

    Depends on ``_restore_process_environment`` so that fixture snapshots the
    environment *before* these variables are set. Without the ordering its
    snapshot included this test's own tmp-path database URL, and because its
    teardown can run after monkeypatch has already undone the set, it put the
    stale value back permanently -- leaking one test's temporary database into
    every suite that ran afterwards.
    """

    for name in SHELL_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("HARBORRAG_HOST", "127.0.0.1")
    monkeypatch.setenv("HARBORRAG_ENV", "dev")
    # Every no-auth test client calls as one principal; tests that need limits set them.
    monkeypatch.setenv("HARBORRAG_HTTP_RATE_DEFAULT", "100000")
    monkeypatch.setenv("HARBORRAG_HTTP_RATE_CHAT", "100000")
    monkeypatch.setenv(
        "HARBORRAG_CONTROL_DB_URL",
        f"sqlite+aiosqlite:///{tmp_path}/control.db",
    )


@pytest.fixture(autouse=True)
def _restore_process_environment() -> Iterator[None]:
    """Undo environment mutations made by production code under test.

    Project activation seeds HARBORRAG_* defaults and .env values straight into
    ``os.environ`` (monkeypatch cannot see those writes), so restore a snapshot after
    every test to keep them from leaking into the next one.
    """

    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture(autouse=True)
def _restore_working_directory() -> Iterator[None]:
    """Undo the process-wide chdir that project (and checkout) activation performs.

    ``activate_project`` calls ``os.chdir`` directly, which monkeypatch cannot see, so a
    test that activates a project would otherwise leave every later test running from that
    directory.
    """

    saved = os.getcwd()
    yield
    os.chdir(saved)


@pytest.fixture
def cli_project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Run `cli.main` inside a project of this test's own.

    Without this the CLI's project gate rejects the command, and the tests only passed
    because a repository checkout supplies catalogs from the CWD -- which made them pass
    from the repository root and fail from ``packages/harborrag-app`` (how CI runs them).
    ``HARBORRAG_PROJECT`` selects the project explicitly, so discovery neither walks up out
    of ``tmp_path`` nor depends on where pytest was started.
    """

    root = tmp_path / "project"
    root.mkdir()
    (root / "harborrag.yaml").write_text("runtime: {}\n", encoding="utf-8")
    monkeypatch.setenv("HARBORRAG_PROJECT", str(root))
    return root


@pytest.fixture(autouse=True)
def _reset_active_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts without a project activated by a previous `cli.main` call.

    Resets the activated project, the directory it was activated from and the stack tenant
    the environment load recorded.
    `monkeypatch.setattr` rather than a plain assignment: if any is ever renamed,
    this raises instead of quietly creating a new attribute and letting every test share
    process-level activation state again.
    """

    monkeypatch.setattr(project_module, "_ACTIVE", None)
    monkeypatch.setattr(project_module, "_INVOCATION", None)
    monkeypatch.setattr(environment_module, "_stack_tenant_id", None)


@pytest.fixture(autouse=True)
def _plain_cli_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Help and usage errors print without colour codes, on CI as on a laptop.

    Typer forces a colour terminal when ``GITHUB_ACTIONS`` is set, and the escape
    codes then split the option names the tests look for.
    """
    monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", False)


@pytest.fixture
def absent_connectors_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the one-time connector import at a file that does not exist."""
    path = tmp_path / "absent-connectors.yaml"
    monkeypatch.setenv("HARBORRAG_CONNECTOR_CONFIG_PATH", str(path))
    return path
