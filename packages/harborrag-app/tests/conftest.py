"""Security-safe process defaults shared by API package tests."""

from __future__ import annotations

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
