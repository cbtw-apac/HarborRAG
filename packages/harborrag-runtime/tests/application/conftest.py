"""Process isolation for the application-service tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_application_environment(
    _restore_process_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Use loopback auth and a durable database isolated to each test.

    Pins ``COLUMNS`` and the ``HARBORRAG_*`` defaults the application services read. It
    depends on ``_restore_process_environment`` so that fixture snapshots the environment
    before these values are set; otherwise its teardown could restore this test's own
    tmp-path database URL into every later suite.
    """

    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("HARBORRAG_HOST", "127.0.0.1")
    monkeypatch.setenv("HARBORRAG_ENV", "dev")
    monkeypatch.setenv("HARBORRAG_HTTP_RATE_DEFAULT", "100000")
    monkeypatch.setenv("HARBORRAG_HTTP_RATE_CHAT", "100000")
    monkeypatch.setenv(
        "HARBORRAG_CONTROL_DB_URL",
        f"sqlite+aiosqlite:///{tmp_path}/control.db",
    )


@pytest.fixture(autouse=True)
def _restore_process_environment() -> Iterator[None]:
    """Undo environment mutations made by production code under test."""

    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture(autouse=True)
def _restore_working_directory() -> Iterator[None]:
    """Undo a process-wide chdir made by production code under test."""

    saved = os.getcwd()
    yield
    os.chdir(saved)
