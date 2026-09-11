"""Security-safe process defaults shared by API package tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from harborrag_app.cli import project as project_module


@pytest.fixture(autouse=True)
def _isolated_application_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Use loopback auth and a durable database isolated to each test."""

    monkeypatch.setenv("HARBORRAG_HOST", "127.0.0.1")
    monkeypatch.setenv("HARBORRAG_ENV", "dev")
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

    `monkeypatch.setattr` rather than a plain assignment: if `_ACTIVE` is ever renamed,
    this raises instead of quietly creating a new attribute and letting every test share
    process-level activation state again.
    """

    monkeypatch.setattr(project_module, "_ACTIVE", None)
