"""Memory retention settings reach the policy the memory layer writes with (R5-02)."""

from __future__ import annotations

import os

import pytest

from harborrag_memory import MemoryRetention
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory.policy import memory_policy_from_settings

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_ambient_harborrag_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decide these cases from their arguments, never from the ambient environment."""

    for name in [key for key in os.environ if key.startswith("HARBORRAG_")]:
        monkeypatch.delenv(name, raising=False)


def test_default_retention_settings_reach_the_policy() -> None:
    policy = memory_policy_from_settings(RuntimeSettings())

    assert policy.retention == MemoryRetention(session_days=90, user_days=365, project_days=0)


def test_retention_is_configured_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARBORRAG_MEMORY_RETENTION_DAYS_SESSION", "7")
    monkeypatch.setenv("HARBORRAG_MEMORY_RETENTION_DAYS_USER", "30")
    monkeypatch.setenv("HARBORRAG_MEMORY_RETENTION_DAYS_PROJECT", "180")

    policy = memory_policy_from_settings(RuntimeSettings())

    assert policy.retention == MemoryRetention(session_days=7, user_days=30, project_days=180)


def test_the_unimplemented_pii_redaction_flag_is_gone() -> None:
    assert "memory_pii_redaction" not in RuntimeSettings.model_fields
