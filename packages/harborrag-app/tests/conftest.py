"""Security-safe process defaults shared by API package tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest


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
def _reset_active_project() -> Iterator[None]:
    """Each test starts without a project activated by a previous `cli.main` call."""

    from harborrag_app.cli import project as project_module

    project_module._ACTIVE = None  # noqa: SLF001 - test isolation of process-level state
    yield
    project_module._ACTIVE = None
