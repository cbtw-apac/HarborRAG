"""Security-safe process defaults shared by API package tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _loopback_api_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit-test applications use disabled development auth only on loopback."""

    monkeypatch.setenv("HARBORRAG_HOST", "127.0.0.1")
