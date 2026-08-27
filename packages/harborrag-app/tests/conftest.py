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
    # The API lifespan parses config/models.yaml at startup so a missing
    # ${HARBOR_*} reference fails the boot instead of a request. Tests that
    # bring up the real app therefore need the catalogue to resolve; these
    # placeholders never leave the process because no provider call is made.
    monkeypatch.setenv("HARBOR_CHAT_PROVIDER", "openai")
    monkeypatch.setenv("HARBOR_CHAT_MODEL", "openai/test-chat-model")
    monkeypatch.setenv("HARBOR_CHAT_API_KEY", "test-chat-key")
    monkeypatch.setenv("HARBOR_EMBED_PROVIDER", "openai")
    monkeypatch.setenv("HARBOR_EMBED_MODEL", "openai/test-embed-model")
    monkeypatch.setenv("HARBOR_EMBED_API_KEY", "test-embed-key")
